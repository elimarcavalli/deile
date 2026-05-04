# Fase 3 — Bridge, capabilities, formatters, pipelines

> Esta fase costura tudo: ingress pipeline + agent bridge (in-process e oneshot) + capability catalog + persona selector + output formatters + DLQ + métricas + egress pipeline. No fim, a foundation é capaz de receber `MessageEnvelope`, decidir, invocar DEILE, formatar e devolver via `ProviderAdapter` — usando o `FakeProviderAdapter` da fase 1.

## Pré-requisitos

- Fase 1 e 2 mergeadas e verdes.
- Compreensão das mudanças no DEILE descritas em [`../../deile/00-PLAN.md`](../../deile/00-PLAN.md). Algumas dependências do bridge dependem de hooks no `DeileAgent` — esta fase pode usar adaptadores temporários até o plano DEILE entregar.
- `deilebot/runtime/` criado nesta fase com `webhook_router.py` (ABC + dispatcher), `single_runtime.py` (loop adapter único). `webhook_server.py` (FastAPI concreto) é introduzido pelo primeiro adapter HTTP-only — Telegram fase 2 ou WhatsApp fase 1.

## Entregáveis

### 3.1. `foundation/output_formatter.py` — Renderer abstrato + plain

```python
class OutputFormatter(ABC):
    name: str
    max_message_chars: int

    @abstractmethod
    def render(self, ast: MarkupAST) -> str: ...

    def split(self, text: str) -> list[str]:
        """Split em pedaços ≤ max_message_chars respeitando linha/parágrafo/codeblock."""

class PlainTextFormatter(OutputFormatter):
    """Strip de tudo que é formatação. Útil para Messenger/Instagram."""
    ...
```

Renderers de provider (Discord, Telegram, WhatsApp) ficam em `providers/<x>/formatter.py`.

### 3.2. `foundation/persona_selector.py`

```python
class PersonaSelector:
    def __init__(self, settings: BotSettings, identity: IdentityResolver): ...

    async def resolve(
        self,
        env: MessageEnvelope,
        bot_user: BotUser,
        is_owner: bool,
    ) -> str:
        """Retorna o nome da persona DEILE a usar (ex.: 'developer', 'host', 'debugger')."""
```

Resolução: settings declaram regras em ordem (primeira que casar vence):

```yaml
personas:
  default: developer
  rules:
    - when: { provider: discord, scope: DM, owner: true }
      use: developer
    - when: { provider: discord, scope: GROUP, channel_name_in: ["geral"] }
      use: host
    - when: { provider: telegram, scope: DM }
      use: developer
```

### 3.3. `foundation/capabilities.py` — `CapabilityCatalog`

Já existe `ProviderCapabilities` (fase 1). Aqui adicionamos:

```python
@dataclass(frozen=True, slots=True)
class CapabilitySnapshot:
    provider: str
    provider_capabilities: ProviderCapabilities
    cogs_or_handlers: list[str]              # nomes de cogs/handlers carregados
    agent_tools: list[ToolMeta]              # nome + descrição curta + risco
    agent_models: list[str]                  # provider:model_id disponíveis
    persona_default: str
    bot_settings_summary: dict               # subset não-sensitive de settings

class CapabilityCatalog:
    def __init__(self, settings: BotSettings): ...

    async def snapshot(self, adapter: ProviderAdapter, agent_meta_provider: AgentMetaProvider) -> CapabilitySnapshot: ...

    def render_for_system_prompt(self, snap: CapabilitySnapshot) -> str:
        """String pronta para colar em <bot_capabilities> no system prompt."""

    def render_for_user(self, snap: CapabilitySnapshot, formatter: OutputFormatter) -> str:
        """MarkupAST → string para ser enviada via /capabilities."""
```

`AgentMetaProvider` é uma ABC em `deilebot/foundation/agent_meta.py` com:

```python
class AgentMetaProvider(ABC):
    @abstractmethod
    async def list_tools(self) -> list[ToolMeta]: ...
    @abstractmethod
    async def list_models(self) -> list[str]: ...        # ['anthropic:claude-...', 'deepseek:deepseek-chat', ...]
    @abstractmethod
    async def list_personas(self) -> list[str]: ...
```

Implementação concreta `DeileAgentMetaProvider` vive em `deilebot/foundation/agent_meta.py` também, encostando em `agent.tool_registry`, `model_router`, `persona_manager`. Implementação fake `FakeAgentMetaProvider` em `_testing.py` para uso nos testes E2E da fase 4.

`ToolMeta` (DTO):

```python
@dataclass(frozen=True, slots=True)
class ToolMeta:
    name: str
    description: str
    risk_level: Literal["safe", "low", "medium", "high"] = "safe"
    requires_bot_context: bool = False           # True para BotTool
```

### 3.4. `foundation/agent_bridge.py` — Bridge para o DEILE

```python
@dataclass(frozen=True)
class AgentInvocation:
    bot_user_id: str
    persona: str
    forced_model: Optional[str]
    inbound_text: str                # texto já normalizado/limpo
    inbound_attachments: tuple[Attachment, ...]
    history: list[StoredMessage]     # contexto recente (já filtrado)
    capabilities: CapabilitySnapshot
    extra_system_prompt: str         # bloco <bot_capabilities>
    timeout_seconds: int

@dataclass(frozen=True)
class AgentResponse:
    text: str                        # texto plano
    markup: MarkupAST                # AST renderizada
    tool_calls: list[ToolCallRecord] # debug/audit
    elapsed_ms: int
    model_used: str
    truncated: bool

class AgentBridge(ABC):
    @abstractmethod
    async def invoke(self, inv: AgentInvocation) -> AgentResponse: ...

class InProcessAgentBridge(AgentBridge):
    """Mantém um DeileAgent global. Cria sessão por bot_user_id (estável)."""
    def __init__(self, agent_provider: Callable[[], Awaitable[DeileAgent]]): ...

class OneshotSubprocessAgentBridge(AgentBridge):
    """Roda `python3 deile.py` --model X "<msg>" via subprocess, lê stdout, parse markdown → AST."""
    def __init__(self, deile_py_path: Path = Path("deile.py")): ...

def build_agent_bridge(settings: FoundationSettings) -> AgentBridge: ...
```

Detalhes obrigatórios do `InProcessAgentBridge`:

- **Sessão por `bot_user_id`** com convenção `bot_session_<bot_user_id>` — exige que a feature de "session_id externo" do DEILE esteja entregue (ver plano DEILE Fase 1). Se ainda não entregue, fallback para `oneshot_cli_session` por inv (perde memória entre turnos — declarar isso no log).
- **Timeout obrigatório** via `asyncio.wait_for(...)`. Se estourar, levantar `AgentInvocationTimeout` e adicionar à DLQ.
- **Captura de exceções amplas**: qualquer `Exception` do agent vira `AgentInvocationError(context={...})` — o adapter nunca crasha por erro do agente.
- **Streaming opcional**: se DEILE emitir streaming (feature `feature/streaming-ui`), expor `invoke_stream(inv) -> AsyncIterator[StreamChunk]` para o adapter atualizar mensagem progressivamente. Inicialmente o `IngressPipeline` consome só o final; streaming é opt-in por adapter na fase Discord.

Detalhes do `OneshotSubprocessAgentBridge`:

- Comando: `python3 deile.py --model {forced_model} -- {inbound_text}` (cwd = raiz do projeto).
- `extra_system_prompt` é injetado via env `DEILE_EXTRA_SYSTEM_PROMPT` (precisa de hook no DEILE — ver plano DEILE Fase 2).
- Stdin: pode passar texto via stdin se preferir, evita escape.
- Stdout = resposta. Stderr = log.
- Sem sessão persistente (cada chamada é fresh). É o trade-off do isolamento.

### 3.5. `foundation/dlq.py` — Dead-Letter Queue

```python
class DLQRecord(TypedDict): ...

class DeadLetterQueue:
    def __init__(self, store: ConversationStore, settings: BotSettings): ...

    async def enqueue(self, provider: str, payload: dict, error: str, attempts: int) -> None: ...
    async def replay(self, *, provider: Optional[str] = None, since: Optional[datetime] = None, dry_run: bool = False) -> list[DLQRecord]: ...
    async def list_pending(self, limit: int = 100) -> list[DLQRecord]: ...
    async def purge(self, older_than_days: int) -> int: ...
```

Operação:

- Toda falha de `send_message` em `EgressPipeline` após N retries (default 3) vai para a DLQ.
- Comando admin `/dlq list` e `/dlq replay` (na fase Discord).
- Replay busca via reflexão o `EgressPipeline` da foundation e re-tenta.

### 3.6. `foundation/event_bus.py` — Wrapper

```python
class BotEventType(str, Enum):
    INBOUND_RECEIVED = "bot.inbound.received"
    SHOULD_RESPOND_DECIDED = "bot.intent.decided"
    AGENT_INVOKED = "bot.agent.invoked"
    AGENT_RESPONDED = "bot.agent.responded"
    AGENT_FAILED = "bot.agent.failed"
    OUTBOUND_SENT = "bot.outbound.sent"
    OUTBOUND_FAILED = "bot.outbound.failed"
    PERMISSION_DENIED = "bot.permission.denied"
    RATE_LIMITED = "bot.rate.limited"
    DLQ_ENQUEUED = "bot.dlq.enqueued"
    DLQ_REPLAYED = "bot.dlq.replayed"

class BotEventBus:
    def __init__(self, deile_bus: DeileEventBus): ...
    async def publish(self, event_type: BotEventType, payload: dict) -> None: ...
```

### 3.7. `foundation/metrics.py` — Coletor

```python
class MetricsCollector:
    """Counters, histograms, gauges — tudo em memória + emitido no event_bus."""
    def inc(self, name: str, labels: dict = {}, value: int = 1): ...
    def observe(self, name: str, labels: dict, value: float): ...
    def gauge(self, name: str, labels: dict, value: float): ...
    def snapshot(self) -> dict: ...                    # para /metrics
```

Métricas obrigatórias:

| Métrica | Tipo | Labels |
|---|---|---|
| `bot_inbound_total` | counter | provider, scope |
| `bot_should_respond_total` | counter | provider, decision (true/false), classifier |
| `bot_agent_invocations_total` | counter | provider, persona, model, status |
| `bot_agent_invocation_seconds` | histogram | provider, model |
| `bot_outbound_total` | counter | provider, status |
| `bot_outbound_chars` | histogram | provider |
| `bot_rate_limited_total` | counter | provider, reason |
| `bot_permission_denied_total` | counter | provider, action |
| `bot_dlq_size` | gauge | provider |
| `bot_dlq_enqueued_total` | counter | provider, error_type |

### 3.8. `foundation/pipeline.py` — `IngressPipeline` e `EgressPipeline`

```python
class IngressPipeline:
    def __init__(
        self,
        identity: IdentityResolver,
        permissions: PermissionGate,
        rate_limit: RateLimiter,
        store: ConversationStore,
        intent: IntentClassifier,
        bridge: AgentBridge,
        capability_catalog: CapabilityCatalog,
        persona_selector: PersonaSelector,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        egress: EgressPipeline,
        agent_meta: AgentMetaProvider,
    ): ...

    async def handle(self, env: MessageEnvelope, adapter: ProviderAdapter) -> None:
        """O ponto único onde um adapter entrega uma mensagem recebida."""
```

Fluxo (idêntico ao §5 do `00-PLAN.md`, agora com tratamento de erro completo):

1. `audit.log(INBOUND_RECEIVED)` + `metrics.inc(bot_inbound_total)`.
2. `user = await identity.resolve(env.author...)`.
3. `decision = await permissions.check(user, INVOKE_AGENT, scope=env.channel.scope)`.
   - `not decision.allowed` → `audit.log(PERMISSION_DENIED)` + `event_bus.publish(...)` + return.
4. `await rate_limit.acquire_inbound(user)` → `RateLimited` vira audit + return.
5. `await store.upsert_user(user)`, `await store.upsert_channel(env.channel)`, `await store.record_inbound(env)`.
6. `history = await store.get_recent_messages(env.channel)`.
7. `intent_dec = await intent.decide(env, history, self_user_id=adapter.self_user_id)`.
   - `not should_respond` → `audit.log(SHOULD_RESPOND_DECIDED, payload={"decision": False})` + return.
8. `is_owner = await permissions.is_owner(user)`.
9. `persona = await persona_selector.resolve(env, user, is_owner)`.
10. `snap = await capability_catalog.snapshot(adapter, agent_meta)`.
11. `extra_sys = capability_catalog.render_for_system_prompt(snap)`.
12. `inv = AgentInvocation(...)`.
13. `audit.log(AGENT_INVOKED)`.
14. `try: response = await bridge.invoke(inv)`.
    - on error: `audit.log(AGENT_FAILED)`, fallback message via `egress.send_fallback(adapter, env.channel, env.message_id, "agent_failed")`, return.
15. `audit.log(AGENT_RESPONDED, payload={"elapsed_ms": ..., "tools": [...]})`.
16. `await egress.send_response(adapter, env, response, persona)`.

```python
class EgressPipeline:
    def __init__(
        self,
        formatters: dict[str, OutputFormatter],
        rate_limit: RateLimiter,
        store: ConversationStore,
        audit: BotAuditLogger,
        event_bus: BotEventBus,
        metrics: MetricsCollector,
        dlq: DeadLetterQueue,
        retry_policy: RetryPolicy,
    ): ...

    async def send_response(self, adapter: ProviderAdapter, env: MessageEnvelope, response: AgentResponse, persona: str) -> None: ...
    async def send_fallback(self, adapter: ProviderAdapter, channel: Channel, reply_to: str, reason: str) -> None: ...
```

Detalhes:

- Render → split (se exceder `max_message_chars` do provider) → enviar cada chunk com reply ao msg original do primeiro chunk e replies ao chunk anterior nos seguintes.
- Retry com backoff exponencial via `tenacity` (3 tentativas, jitter).
- Falha definitiva → `DLQ.enqueue(...)`.

### 3.9. Testes desta fase

| Módulo | Casos |
|---|---|
| `output_formatter` | `PlainTextFormatter` strip; `split` em chunks respeitando codeblocks (não cortar dentro de ```...``` pela metade) |
| `persona_selector` | Casamento de regras na ordem; default quando nada casa |
| `capability_catalog` | snapshot com `FakeProviderAdapter` + `FakeAgentMetaProvider`; render produz string previsível |
| `agent_bridge` | `OneshotSubprocessAgentBridge` testado com `deile.py` real e modelo `deepseek` (marcado `slow`); `InProcessAgentBridge` com `FakeAgent` (objeto mock) |
| `dlq` | enqueue → list_pending → replay (com adapter mock que ainda falha; com adapter mock que aceita) |
| `event_bus` | publish chega no `DeileEventBus` mockado |
| `metrics` | inc/observe/gauge funciona; snapshot serializável |
| `pipeline` (E2E parcial) | `FakeProviderAdapter.inject(envelope)` → eventualmente `FakeProviderAdapter.inbox` ganha resposta; cada step do fluxo testável isoladamente com mocks |

Total esperado: ~50 testes, ~12 marcados `slow` (custam tokens reais).

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest deilebot/tests/foundation/ -v` passa, com testes `slow` rodando opcionalmente via `pytest -m slow` |
| AC-2 | `FakeProviderAdapter.inject(env)` → `await asyncio.sleep(...)` → `assert FakeProviderAdapter.inbox` contém resposta gerada por DEILE real (`-m slow`) |
| AC-3 | DLQ: forçar 3 falhas de `send_message` → registro aparece na tabela `dlq`; `replay()` reenvia |
| AC-4 | `extras_system_prompt` contém lista de tools do agente (verificável por contains nos testes) |
| AC-5 | Métricas: após 1 inbound + 1 outbound, `metrics.snapshot()` mostra contadores correspondentes |
| AC-6 | `audit` table tem entradas para o ciclo completo |
| AC-7 | Sem regressão no CLI (`python3 deile.py "olá"`) |

## Pontos de atenção

- **Não** acoplar o pipeline a Discord/Telegram aqui. Único ponto de injeção é `adapter: ProviderAdapter`.
- **`extra_system_prompt`** depende do hook DEILE (plano DEILE fase 2). Se não estiver pronto, o bridge in-process injeta via `session.context_data["extra_system_prompt"]` e o agente, num passo intermediário, lê isso. Documentar a transição.
- **Streaming de resposta** fica como `invoke_stream` na ABC, mas o pipeline default consome só o final. Discord adapter (na fase 3 do plano discord) vai consumir streaming para `message.edit` progressivo.
- **Relógio**: usar `datetime.now(timezone.utc)` em todo lugar. `time.monotonic()` para medir elapsed.
- **Testes `slow`** custam tokens reais — orientar dev a rodar com modelo `deepseek-chat` (mais barato) e a usar `pytest -m slow -k <específico>` para não estourar.

## Estimativa de esforço

3–4 dias. `agent_bridge` + pipeline + integração com `DeileAgent` é o pedaço delicado.
