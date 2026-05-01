# Plano — Streaming de tool-calls em tempo real (DEILE → Claude-Code-like UX)

> **Status**: documento de planejamento (pré-implementação). Após o feature ser entregue, um documento datado seguindo o template de 14 seções de `claude_dev/7_documentation_directives.md` será gerado em `docs/YYMMDD_HHMM_streaming_ui.md`.

**Objetivo:** transformar o turno do DEILE de uma renderização única no fim em um transcript progressivo, com cada tool-call (Bash, Read, Write, Edit, Search, Git, etc.) e cada bloco de texto do assistente aparecendo no terminal **no momento em que ocorrem**, no estilo do segundo print de referência (Claude Code).

**Tier de escopo (doc 5):** Large — toca ≥ 2 subpacotes (`deile/core/models/`, `deile/core/`, `deile/ui/`, `deile/events/`), introduz contrato público novo, exige aprovação antes de Phase 3.

**Princípios aplicáveis (doc 4):** Asynchronous by Design (todo o stream é `async for`), Clean Architecture (UI consome um contrato; tool-loop deixa de ser responsabilidade dos providers), UI/UX Excellence (progressive disclosure), DRY (eliminação da duplicação entre os 4 providers).

---

## Diagnóstico — duplicação que motivou a refatoração

A primeira versão deste plano propunha replicar streaming em cada provider. Auditoria do código revelou que **isso perpetuaria um problema arquitetural pré-existente**: cada provider hoje carrega sua própria cópia do tool-loop:

| Arquivo | Método | Responsabilidade duplicada |
|---|---|---|
| `anthropic_provider.py:210-323` | `chat_with_tools` | Tool-loop com iteração + execução |
| `openai_provider.py` | `chat_with_tools` | Idem |
| `deepseek_provider.py` | `chat_with_tools` | Idem (herda OpenAI) |
| `gemini_provider.py:681+` | `_gemini_chat_with_tools` | Idem |

Cada um reimplementa: `for iteration in range(MAX) → call model → coletar tool_use → executar via tool_registry → append result → repete`. **O que é genuinamente diferente** entre providers é só (a) a chamada SDK, (b) o shape da mensagem `tool_result` no histórico, (c) o shape dos chunks de stream.

**Decisão arquitetural deste plano:** o tool-loop deixa de ser responsabilidade dos providers e passa a viver no agent (`ToolLoopExecutor`). Providers expõem apenas o que é genuinamente SDK-específico:
- `generate_stream(messages, tools=...)` — estendido para aceitar `tools=` e emitir `TOOL_USE_*`. **Não executa tools.**
- `format_tool_result_message(tool_call_id, tool_name, payload) -> ModelMessage` — adapter que encoda o resultado no shape que aquele SDK espera de volta.

Resultado: lógica de orquestração centralizada, providers ~50% menores, drift entre providers eliminado.

---

## Fase 0 — Contrato de stream (1 arquivo, ~30 linhas)

**Por quê primeiro:** o agent, os 4 providers e a UI dependem deste contrato. Definir antes evita retrabalho.

**Arquivo:** `deile/core/models/stream_events.py` (estender, não recriar)

**Mudanças:**
- Adicionar `TOOL_RESULT` ao `StreamEventType` enum (resultado da execução, separado do `TOOL_USE_END` que significa "modelo terminou de emitir o tool_call"). Sem ele a UI não distingue "modelo pediu a tool" de "tool executou e retornou".
- Adicionar campos no `UnifiedStreamEvent`:
  - `tool_status: Optional[str]` — `"success" | "error" | "running"`.
  - `tool_result_summary: Optional[str]` — preview curto (≤ 200 chars) do payload, para renderizar inline.
  - `tool_result_data: Optional[Any]` — payload bruto, para a UI decidir display rico (e.g. tabela para `list_files`).
  - `iteration: Optional[int]` — qual iteração do tool-loop emitiu esse evento (debug e métricas).

**Compatibilidade:** todos os campos `Optional`, default `None` → não quebra `generate_stream` existente.

---

## Fase 1 — Providers ficam mais finos: streaming tool-aware + adapter

**Princípio:** providers nunca mais chamam `tool_registry.execute_tool(...)`. Eles **avisam** "o modelo pediu a tool X com args Y" via stream e sabem como **encodar** o resultado de volta. Quem orquestra o loop é o agent (Fase 2).

### 1.1 `deile/core/models/base.py` (`ModelProvider`)

Estender a assinatura existente:
```python
async def generate_stream(
    self,
    messages: List[ModelMessage],
    system_instruction: Optional[str] = None,
    tools: Optional[List[ToolSchema]] = None,   # ← novo
    **kwargs: Any,
) -> AsyncIterator[UnifiedStreamEvent]: ...
```

Adicionar método novo (concrete, não abstrato):
```python
def format_tool_result_message(
    self,
    tool_call_id: str,
    tool_name: str,
    payload: Any,
) -> ModelMessage: ...
```

Default na base: levanta `NotImplementedError` se o provider for usado num caminho com tools sem ter sobrescrito.

### 1.2 Cada provider — `generate_stream` ganha branch tool-aware

Estimativa de tamanho por provider:

| Provider | Linhas hoje (`chat_with_tools`) | Linhas após (`generate_stream` tool-aware) | Linhas do `format_tool_result_message` |
|---|---|---|---|
| Anthropic | ~115 | ~50 (adiciona branch para `tool_use` blocks no stream existente) | ~10 |
| OpenAI | ~100 | ~40 | ~8 |
| DeepSeek | herda OpenAI | herda OpenAI | herda OpenAI |
| Gemini | ~120 | ~60 (mais delicado — ver 1.3) | ~10 |

**Anthropic** (`anthropic_provider.py`):
- `messages.stream(..., tools=anthropic_tools)` quando `tools` for passado.
- `content_block_delta` com `text_delta` → `TEXT_DELTA`.
- `content_block_start` com `tool_use` → `TOOL_USE_START(tool_call_id, tool_name)`.
- `input_json_delta` → `TOOL_USE_DELTA` *(opcional, ver decisão #1)*.
- `content_block_stop` em bloco `tool_use` → `TOOL_USE_END(tool_call_id, arguments=parsed)`.
- `message_stop` → `USAGE_FINAL`. **Não executa tool nenhuma.** O agent decide.
- Em erro: `ERROR(error_envelope=...)`.

**OpenAI / DeepSeek**: stream já existe; adicionar `tools=`/`tool_choice="auto"` no `chat.completions.create(stream=True)`. Mapeamento:
- `delta.content` → `TEXT_DELTA`.
- `delta.tool_calls[i].function.name` (primeira aparição) → `TOOL_USE_START`.
- `delta.tool_calls[i].function.arguments` (chunks) → acumular; emitir `TOOL_USE_END` no `finish_reason == "tool_calls"`.

**Gemini**: ver Fase 1.3.

### 1.3 Gemini — investigação obrigatória antes de codar

O `_gemini_chat_with_tools` atual usa `chat.send_message()` (síncrono awaitable). Ações:
1. Verificar se `google.genai` SDK suporta `chat.send_message_stream()` **com** `tools=`/function-calling habilitado. Documentação ou teste empírico.
2. **Se sim**: mapeamento idêntico aos outros — emite `TEXT_DELTA` e `TOOL_USE_START/END` à medida que chunks chegam.
3. **Se não**: degradação documentada — `generate_stream` para Gemini emite eventos "lumpy" (chama `send_message`, ao receber resposta com `function_call` parts, emite `TOOL_USE_START` + `TOOL_USE_END` em rajada). Sem `TEXT_DELTA` granular char-a-char, mas cada **rodada** do loop ainda surge progressivamente — UX continua significativamente melhor que hoje.

### 1.4 `format_tool_result_message` por provider

Encoda o payload de retorno no shape SDK-específico:
- **Anthropic**: `ModelMessage(role="user", content=[{"type": "tool_result", "tool_use_id": ..., "content": [{"type": "text", "text": json.dumps(payload)}]}])`.
- **OpenAI/DeepSeek**: `ModelMessage(role="tool", content=json.dumps(payload), metadata={"tool_call_id": ...})`.
- **Gemini**: `ModelMessage(role="user", content=..., metadata={"function_response": {"name": tool_name, "response": payload}})` — o agent passa para o provider que reconstrói o `Part(function_response=...)` na hora de enviar.

### 1.5 Deprecação do `chat_with_tools` antigo

**Estratégia conservadora:** transformar `chat_with_tools` em wrapper trivial sobre o novo caminho:
```python
async def chat_with_tools(self, ...):
    """DEPRECATED: use ToolLoopExecutor + generate_stream(tools=...)."""
    # Apenas roda o loop do agent internamente e agrega o stream em tupla
    return await _legacy_chat_with_tools_adapter(self, ...)
```
Mantém testes verdes e chamadores legados (e.g. `_generate_response_with_function_calling_legacy`) sem alteração. Remoção do wrapper fica para um PR seguinte, depois que `process_input_stream` provar estabilidade.

---

## Fase 2 — Agent absorve o tool-loop (escrito uma vez)

**Arquivo:** `deile/core/agent.py`

### 2.1 Novo: `_run_tool_loop_stream` (ou classe `ToolLoopExecutor`)

```python
async def _run_tool_loop_stream(
    self,
    provider: ModelProvider,
    messages: List[ModelMessage],
    tools: List[ToolSchema],
    system_instruction: Optional[str],
    session: AgentSession,
) -> AsyncIterator[UnifiedStreamEvent]:
    """Tool-loop unificado. Roda até o modelo parar de pedir tools ou MAX_ITERATIONS."""
    for iteration in range(MAX_TOOL_ITERATIONS):
        pending_tool_calls: list[tuple[str, str, dict]] = []  # (id, name, args)

        async for event in provider.generate_stream(messages, system_instruction, tools=tools):
            # Anota a iteração para a UI poder exibir
            event.iteration = iteration
            yield event   # repassa text deltas, tool_use_start/delta/end direto à UI

            if event.type is StreamEventType.TOOL_USE_END:
                pending_tool_calls.append(
                    (event.tool_call_id, event.tool_name, event.arguments or {})
                )

        if not pending_tool_calls:
            return   # modelo encerrou sem pedir mais tools

        # Append do "assistant turn com tool_use" (cada provider sabe encodar)
        messages.append(provider.format_assistant_tool_use_message(pending_tool_calls))

        # Executa cada tool e emite TOOL_RESULT para a UI
        for tool_call_id, tool_name, args in pending_tool_calls:
            ctx = ToolContext(args=args, working_directory=session.working_directory, ...)
            try:
                result = await self.tool_registry.execute_tool(tool_name, ctx)
                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_RESULT,
                    tool_call_id=tool_call_id,
                    tool_name=tool_name,
                    tool_status="success" if result.is_success else "error",
                    tool_result_summary=_summarize(result, max_chars=200),
                    tool_result_data=result.data,
                    iteration=iteration,
                )
                messages.append(provider.format_tool_result_message(tool_call_id, tool_name, result))
                # Auditoria não-bloqueante via EventBus (ver 2.5)
                await self._publish_tool_event("tool.completed", tool_name, result)
            except Exception as exc:
                yield UnifiedStreamEvent(
                    type=StreamEventType.TOOL_RESULT,
                    tool_call_id=tool_call_id, tool_name=tool_name,
                    tool_status="error", tool_result_summary=str(exc)[:200],
                    iteration=iteration,
                )
                messages.append(provider.format_tool_result_message(tool_call_id, tool_name, {"error": str(exc)}))
                await self._publish_tool_event("tool.failed", tool_name, error=exc)

    logger.warning("Tool loop hit MAX_TOOL_ITERATIONS=%d", MAX_TOOL_ITERATIONS)
```

**Esta é a única implementação do tool-loop em todo o codebase após esta refatoração.** Adicionar um 5º provider no futuro requer só `generate_stream(tools=...)` + `format_tool_result_message` + `format_assistant_tool_use_message` — zero lógica de iteração nova.

### 2.2 Novo: `process_input_stream(...) -> AsyncIterator[UnifiedStreamEvent]`

Espelha `process_input` (linhas 303-415 atuais) mas yields eventos. Reusa toda a lógica de:
- Slash commands (yield um `TEXT_DELTA` com o output e finaliza).
- Sessão / histórico / parsing / proactive tools / workflow detection.
- `forced_model` / cascade / budget guard.
- Persona / autonomous processing.

A parte que substitui `_process_iterative_function_calling`:
```python
async for event in self._run_tool_loop_stream(provider, messages, tools, system_instruction, session):
    yield event
```

### 2.3 `process_input` continua existindo (compatibilidade)

Implementação trivial: consome o próprio stream internamente, agrega `TEXT_DELTA` em `content`, agrega `TOOL_RESULT` em `tool_results`, retorna `AgentResponse`. Mantém `_run_oneshot` e testes em `deile/tests/might/` funcionando sem alteração.

```python
async def process_input(self, ...) -> AgentResponse:
    content_buf = []
    tool_results = []
    async for event in self.process_input_stream(...):
        if event.type is StreamEventType.TEXT_DELTA:
            content_buf.append(event.text)
        elif event.type is StreamEventType.TOOL_RESULT:
            tool_results.append(_event_to_tool_result(event))
    return AgentResponse(content="".join(content_buf), tool_results=tool_results, ...)
```

### 2.4 `_apply_validation_gate`

Continua sendo síncrono awaitable após o stream esgotar. O gate precisa do `content` final + lista de `tool_results` — `process_input_stream` agrega esses pedaços ao consumir o próprio loop e só aplica o gate uma vez antes de encerrar. Se o gate **modificar** `content` (e.g. anexar nota anti-alucinação), emite o delta extra como `TEXT_DELTA` final marcado com metadata de origem `validation_gate`, para a UI estilizar como "P.S.".

### 2.5 Eventos de auditoria no EventBus

Em paralelo ao stream (não-bloqueante via `asyncio.create_task`), publicar no `EventBus` global:
- `EventType.TOOL_INVOKED` — emitido em `TOOL_USE_END` (modelo pediu).
- `EventType.TOOL_COMPLETED` — em `TOOL_RESULT` com `success`.
- `EventType.TOOL_FAILED` — em `TOOL_RESULT` com `error`.

Adicionar em `events/event_bus.py:EventType`:
```python
TOOL_INVOKED = "tool.invoked"
TOOL_COMPLETED = "tool.completed"
TOOL_FAILED = "tool.failed"
```

**Por que separar do stream do turno**: o EventBus tem fila/workers/dead-letter — overhead que **não pode** atrasar o render UI. Stream do turno é um `AsyncIterator` direto (latência sub-ms). Bus é para subscritores assíncronos (audit logger, memória episódica, métricas) que reagem quando dá tempo.

---

## Fase 3 — UI consome o stream e renderiza ao vivo

### 3.1 `deile/ui/streaming_renderer.py` (novo)

Componente isolado que encapsula o estado do transcript (lista de blocos: `[{kind: "text"|"tool", state, ...}]`). Razões para isolar:
- **Testabilidade**: aceita uma `Console(file=StringIO())` injetada, valida saída sem terminal real.
- **Reuso futuro**: modo `--ui=plain` para CI/oneshot.

API:
```python
class StreamingRenderer:
    def __init__(self, console: Console, settings: Settings): ...
    async def render(self, event_stream: AsyncIterator[UnifiedStreamEvent]) -> RenderResult: ...
```

### 3.2 `deile/ui/console_ui.py` — método novo `display_streaming_turn`

```python
async def display_streaming_turn(
    self, event_stream: AsyncIterator[UnifiedStreamEvent]
) -> None:
    """Renderiza um turno do agente progressivamente."""
```

Comportamento:
- Imprime cabeçalho `Deile >` uma vez no início.
- Mantém um `rich.live.Live` com refresh ~10Hz onde o transcript cresce.
- Para cada evento:
  - `TEXT_DELTA` → apenda ao buffer de texto corrente, re-renderiza como `Markdown` (ou texto plano em modo compatível). Buffer flush a cada N chars ou 50ms — o que vier primeiro.
  - `TOOL_USE_START` → fecha bloco de texto corrente; cria bloco novo `● ToolName — running...` (amarelo). Indexa pelo `tool_call_id`.
  - `TOOL_USE_DELTA` (opcional, ver decisão #1) → atualiza preview dos args.
  - `TOOL_USE_END` → atualiza header com args definitivos (`● BashTool(command="pytest -q")`).
  - `TOOL_RESULT` → encontra bloco pelo `tool_call_id`; muda cor (verde/vermelho), troca header (`● BashTool ✓` / `✗`), mostra `tool_result_summary` em dim. Se `tool_result_data` for payload conhecido (e.g. file listing) e `settings.show_tool_details` ligado, renderiza com `DisplayManager._render_tool_output(...)`.
  - `USAGE_FINAL` → footer `:hourglass: 1.23s (provider:model • 1.2k in / 0.4k out • $0.001)`.
  - `ERROR` → painel vermelho com `error_envelope.message`.
- Trata `KeyboardInterrupt` (Ctrl+C) interrompendo o `async for` graciosamente.

**Detalhe importante:** o `with self.ui.show_loading(...)` que envolve o turno em `deile.py:155` precisa **morrer**. Spinner cobrindo a tela impede o `Live` do Rich de renderizar. Substituir por header inline opcional ("DEILE pensando…") que some no primeiro `TEXT_DELTA` ou `TOOL_USE_START`.

### 3.3 `deile.py` — loop interativo consome stream

Em `run_interactive` (linhas 133-210), substituir:
```python
with self.ui.show_loading("Processando..."):
    response = await self.agent.process_input(...)
self.ui.display_response(response.content, ...)
```
por:
```python
event_stream = self.agent.process_input_stream(
    user_input=user_input,
    session_id=self.default_session.session_id,
)
await self.ui.display_streaming_turn(event_stream)
```

Tratamento de `meta.budget_exceeded` / `forced_model_not_registered` migra: `process_input_stream` emite um `ERROR` event no início se o turno é abortado por essas condições; o renderer pinta painel vermelho equivalente. Detecta pelo `error_envelope.error_type`.

**`_run_oneshot`** continua usando `process_input` (não-streaming) e fazendo `print(response.content)` — modo pipe-friendly, não muda.

---

## Fase 4 — Testes (`deile/tests/`)

### 4.1 Unit (sem custo de token)

- `deile/tests/core/models/test_stream_events.py` — valida o novo enum/campos.
- `deile/tests/ui/test_streaming_renderer.py` — alimenta sequência fake de `UnifiedStreamEvent`, `Console(file=StringIO())`, assert que output contém os blocos esperados na ordem certa, e que delta do `validation_gate` aparece estilizado.
- `deile/tests/core/test_tool_loop_executor.py` — **teste central da refatoração**. Mocka um provider que emite uma sequência conhecida de eventos (texto, tool_use_start, tool_use_end, finish), valida que `_run_tool_loop_stream` repassa corretamente, executa tools via registry, emite `TOOL_RESULT`, encerra após MAX_ITERATIONS, etc. Esse é o teste que protege a unificação.
- `deile/tests/core/test_agent_streaming.py` — valida que `process_input_stream` repassa fielmente e que `process_input` (não-streaming) agrega o conteúdo correto.

### 4.2 Provider stream tool-aware (sem custo)

Para cada provider, mockar o SDK e validar que o mapeamento SDK → `UnifiedStreamEvent` está correto **com `tools=` passado**. Cobrir tanto chunks de texto puro quanto chunks de tool-call. Anthropic já tem teste de `generate_stream` (text-only); estender.

### 4.3 Empíricos com LLM real (`deile/tests/might/`)

`deile/tests/might/test_streaming_ui.py` — sobe DEILE programaticamente (mesmo bootstrap de `deile.py`), faz um turno com múltiplos tool-calls (e.g. "leia X.py e me diga quantas linhas tem, depois rode `wc -l X.py` e compare"), captura o stream de eventos, e verifica:
- Pelo menos 1 `TEXT_DELTA` chegou **antes** do primeiro `TOOL_USE_START`.
- Pelo menos 1 `TEXT_DELTA` chegou **entre** dois `TOOL_USE_END`s (a "narrativa" inter-rodada).
- `TOOL_RESULT` aparece para cada `TOOL_USE_END`.
- Tempos: `t(primeiro evento) << t(último evento)` — confirma streaming real, não pseudo-stream.

Orçamento: 1 turno, ≤ $0.01.

---

## Fase 5 — Observabilidade & polish

- `audit_logger` continua registrando — ganha "de graça" porque escutará `EventBus.TOOL_*`.
- `settings.show_tool_details` vira gate só pra o **payload** (preview sempre é mostrado; payload completo só com flag).
- Adicionar setting `streaming.enabled` (default `True`) — permite voltar ao modo blocking se algum provider quebrar em produção. Implementação: `process_input_stream` agrega tudo e yield apenas `TEXT_DELTA` final + `USAGE_FINAL` se `streaming.enabled = False`.

---

## Mudanças por arquivo (resumo, com a refatoração)

| Arquivo | Tipo | Ação |
|---|---|---|
| `deile/core/models/stream_events.py` | **edit** | Adicionar `TOOL_RESULT` ao enum + 4 campos novos no dataclass |
| `deile/core/models/base.py` | **edit** | `generate_stream` aceita `tools=`; declarar `format_tool_result_message` + `format_assistant_tool_use_message` |
| `deile/core/models/anthropic_provider.py` | **edit** | Estender `generate_stream` com tool-aware branch (~50 linhas); add 2 adapters (~20 linhas); `chat_with_tools` vira wrapper trivial |
| `deile/core/models/openai_provider.py` | **edit** | Idem (~40 linhas + 15 adapters) |
| `deile/core/models/deepseek_provider.py` | **edit** | Herda de OpenAI; ajustes mínimos se houver |
| `deile/core/models/gemini_provider.py` | **edit** | Estender `generate_stream` (~60 linhas, com fallback se SDK não suportar streaming+FC); adapters |
| `deile/core/agent.py` | **edit** | Adicionar `_run_tool_loop_stream` (**nova lógica única**, ~80 linhas) + `process_input_stream`; `process_input` vira agregador trivial sobre o stream |
| `deile/events/event_bus.py` | **edit** | Adicionar `EventType.TOOL_INVOKED / TOOL_COMPLETED / TOOL_FAILED` |
| `deile/ui/streaming_renderer.py` | **NEW** | Componente Rich `Live` com transcript |
| `deile/ui/console_ui.py` | **edit** | Adicionar `display_streaming_turn` que delega ao renderer |
| `deile.py` | **edit** | `run_interactive` consome stream; remove `show_loading` cobrindo o turno |
| `deile/tests/core/test_tool_loop_executor.py` | **NEW** | Teste central da unificação |
| `deile/tests/core/test_agent_streaming.py` | **NEW** | Unit |
| `deile/tests/ui/test_streaming_renderer.py` | **NEW** | Unit |
| `deile/tests/core/models/test_*_provider_stream.py` | **NEW** | 4 arquivos, mocks de SDK |
| `deile/tests/might/test_streaming_ui.py` | **NEW** | Empírico com LLM real |

**Estimativa revisada:** ~450-650 linhas de código de produção (vs ~600-900 do plano original duplicado) + ~400 linhas de teste. **Linhas removidas:** os `chat_with_tools` antigos viram wrappers triviais (~10 linhas cada), economia líquida de ~300 linhas removidas dos providers ao longo da deprecação completa.

---

## Riscos e mitigações

1. **SDK do Gemini pode não suportar streaming + function-calling simultâneo.** Mitigação: degradação documentada — `generate_stream` para Gemini emite eventos "lumpy" (start+end+result em rajada na fronteira de cada round-trip), enquanto Anthropic/OpenAI/DeepSeek emitem char-a-char. UX ainda muito melhor que hoje.
2. **Rich `Live` em terminais Windows legados pode bugar.** Mitigação: o renderer detecta `legacy_windows=True` e cai para modo "append-only" (sem refresh in-place — cada bloco printado uma vez quando completo). Mesmo contrato.
3. **`_apply_validation_gate` modifica `content`.** Mitigação: emite delta extra como `TEXT_DELTA` separado, marcado, no fim do stream. UX: "P.S." abaixo da resposta principal. Aceitável.
4. **Cobertura `--cov-fail-under=80`** pode falhar durante o desenvolvimento. Mitigar adicionando testes pari-passu (Fase 4 sai junto, não no fim).
5. **Quebra de chamadores existentes** de `chat_with_tools`. Mitigação: **wrapper trivial** transitório (Fase 1.5) mantém a API antiga viva durante a migração; remoção fica para PR seguinte depois que `process_input_stream` provar estabilidade em uso real.
6. **~~Drift entre providers (um respeita MAX_ITERATIONS, outro não)~~** — **eliminado** pela refatoração: a lógica de iteração existe em um único lugar (`_run_tool_loop_stream`).

---

## Ordem de execução proposta

1. **Fase 0** (contrato) — 1 commit, ~10 min.
2. **Fase 2.1** (`_run_tool_loop_stream` + teste unitário com provider mockado) — **antes** dos providers reais. Garante que o loop centralizado funciona contra um contrato claro. 1 commit.
3. **Fase 1 Anthropic** (estende `generate_stream(tools=...)`, adapters, wrapper de compat) — 1 commit + testes. Smoke test: `python deile.py` com `ANTHROPIC_API_KEY`.
4. **Fase 1 OpenAI/DeepSeek** — 1 commit + testes.
5. **Fase 1 Gemini** — 1 commit + testes (com fallback documentado se SDK limitar).
6. **Fase 2.2-2.5** (resto do agent: `process_input_stream`, `process_input` como agregador, EventBus events) — 1 commit.
7. **Fase 3** (UI) — 1 commit. **Aqui o efeito desejado aparece pela primeira vez no terminal.**
8. **Fase 4.3** (teste empírico com LLM real) — 1 commit.
9. **Fase 5** (polish, settings, audit) — 1 commit.

Cada commit auto-contido, com `pytest` verde antes do push.

---

## Decisões em aberto pra confirmar antes de codar

1. **`TOOL_USE_DELTA` (args sendo "digitados") vale a pena no MVP?** Renderizar args char-a-char é bonito mas custa complexidade. **Recomendo: pular no MVP**, mostrar args só no `TOOL_USE_END`.
2. **Spinner some completamente?** Concordo em remover o `show_loading` global do turno, mas posso manter um header `DEILE pensando…` (sem spinner ANSI) que some no primeiro evento. **Recomendo: header inline dim**.
3. **`process_input_stream` único para todos os caminhos** (slash commands, autonomous processing, chat-with-tools)? Slash commands e autonomous emitem 1 `TEXT_DELTA` final + `USAGE_FINAL`. **Recomendo: sim, unificar** — uma única API pública.
4. **4 providers nesta entrega?** Com a refatoração, cada provider muda só ~70 linhas (não 150). **Recomendo: todos os 4 juntos** — o ganho de eliminar drift entre providers é alto e o custo marginal por provider é baixo.
5. **`chat_with_tools` antigo: wrapper trivial neste PR ou já remoção total?** Wrapper trivial é mais seguro (testes legados continuam passando), remoção total exige migrar todos os chamadores. **Recomendo: wrapper neste PR; remoção em PR seguinte** quando tiver confiança em uso real.
