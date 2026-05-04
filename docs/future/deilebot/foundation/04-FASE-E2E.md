# Fase E2E — Bateria de testes ponta-a-ponta da foundation

> Esta fase **não adiciona features** — só prova que a soma das fases 1+2+3 entrega o contrato prometido. Roda contra `FakeProviderAdapter` (sem dependência de Discord/Telegram/etc.) + `DeileAgent` real com modelo barato.

## Pré-requisitos

- Fases 1, 2, 3 da foundation mergeadas.
- Mudanças DEILE Fase 1 (sessão externa) e Fase 2 (extra_system_prompt) entregues, OU os adaptadores temporários documentados na fase 3.
- API key DeepSeek configurada para os testes que invocam o agente real (`DEEPSEEK_API_KEY`).
- `pytest.ini` com marker `e2e` registrado.

## Estrutura

```
deilebot/tests/e2e/
├── __init__.py
├── conftest.py                   # fixtures: store em tmpdir, fake adapter, agent real
├── test_happy_path.py
├── test_permissions.py
├── test_rate_limit.py
├── test_dlq.py
├── test_persona_routing.py
├── test_intent_modes.py
├── test_capability_snapshot.py
├── test_concurrent_channels.py
└── test_persistence_survives_restart.py
```

Todos os testes marcados `@pytest.mark.e2e` (registrar marker no `pytest.ini`). Rodam via `pytest -m e2e`.

## Cenários obrigatórios

### E2E-1. Happy path

1. `FakeProviderAdapter` injeta envelope: usuário "alice" diz "Olá DEILE, lista 3 fatos sobre Python" em DM.
2. Pipeline ingere → permission ok → rate ok → intent decide responder (DM) → bridge invoca DEILE real (modelo `deepseek-chat`) → response chega.
3. `FakeProviderAdapter.inbox[-1].text` contém pelo menos 50 chars não vazios.
4. `audit` table tem `inbound_received`, `should_respond_decided(true)`, `agent_invoked`, `agent_responded`, `outbound_sent`.
5. `metrics.snapshot()["bot_inbound_total"] == 1`, `bot_outbound_total == 1`, `bot_agent_invocation_seconds` tem 1 sample.

### E2E-2. Memória persistente entre turnos

1. Mesmo `bot_user_id` "alice" envia 3 mensagens em sequência ("meu nome é Alice", "qual meu nome?", obrigada").
2. Resposta da 2ª deve mencionar "Alice".
3. `ConversationStore.get_recent_messages` devolve as 3 inbound + 3 outbound em ordem.

### E2E-3. Permissões

1. Owner manda `/admin debug` → permitido (audit `agent_invoked`).
2. Não-owner manda `/admin debug` → `PERMISSION_DENIED` registrado, sem invocação do agente, sem outbound.
3. Usuário em blocklist manda mensagem → ignorado (sem audit de `agent_invoked`, mas com `permission_denied`).

### E2E-4. Rate limit

1. Disparar 50 envelopes do mesmo usuário em <1s.
2. Após `rate_limit_user_burst`, demais devem ser `rate_limited`.
3. `metrics.bot_rate_limited_total{reason=user_burst}` > 0.
4. Após esperar `60s / refill_per_minute`, novo envelope passa.

### E2E-5. DLQ + replay

1. Setar `FakeProviderAdapter.send_message` para `raise ProviderError` 100% das vezes.
2. Disparar 1 envelope que deve gerar resposta.
3. Após retries, registro aparece em `dlq` table.
4. Curar adapter (não mais raise) e chamar `DeadLetterQueue.replay()`.
5. `inbox` recebe a mensagem; `dlq` esvazia; audit `dlq_replayed` aparece.

### E2E-6. Persona routing

1. Owner em DM Discord → persona "developer".
2. Mesmo owner em canal `#geral` → persona "host".
3. Verificar pelo bloco `extra_system_prompt` montado (capturável pelo `FakeAgent` que registra tudo o que recebe).

### E2E-7. Intent modes

1. Configurar `intent_classifier=heuristic`. Mandar "ok" em group → não responde. Mandar "@bot ajuda" → responde. Mandar em DM → responde.
2. Trocar para `always_respond_to_addressed`. Repetir. Idem.
3. Trocar para `always_respond`. Repetir. Tudo responde.
4. Trocar para `llm`. Mandar "estou triste hoje" em group → LLM decide; resultado guardado para análise.

### E2E-8. Capability snapshot

1. Snapshot do `FakeProviderAdapter` + `FakeAgentMetaProvider` com tools mockadas.
2. Render para system prompt contém: nome dos cogs, lista de tools com descrição curta, lista de modelos disponíveis, persona ativa.
3. Render para usuário (com `PlainTextFormatter`) é legível e ≤ `max_message_chars` do provider.

### E2E-9. Concorrência multi-canal

1. 5 canais paralelos, cada um com 10 envelopes simultâneos.
2. Todos os envelopes processados sem race; `message` table tem 50 inbound + 50 outbound; nenhum duplicado pelo unique constraint.
3. Sem deadlock — total < 60s.

### E2E-10. Persistência sobrevive restart

1. Disparar 5 envelopes processados normalmente.
2. Fechar `IngressPipeline`, `ConversationStore`, `MetricsCollector`.
3. Reabrir tudo no mesmo `sqlite_path`.
4. `get_recent_messages` ainda retorna as 5+5 antigas.
5. `bot_user_id` da Alice é o mesmo (não regerou).

## Fixtures principais (`conftest.py`)

```python
@pytest.fixture
async def store(tmp_path):
    s = ConversationStore(tmp_path / "bot.sqlite")
    await s.init()
    yield s
    await s.close()

@pytest.fixture
async def real_agent():
    """DeileAgent real, bootstrapped com bootstrap_providers (precisa DEEPSEEK_API_KEY)."""

@pytest.fixture
async def fake_adapter(): ...

@pytest.fixture
async def pipeline(store, real_agent, fake_adapter): ...
```

## Regras dos testes E2E

- **Sem mock no agente** quando o cenário precisa de inteligência. Mocks só nos serviços externos do adapter (que não existem ainda nesta fase, então o adapter é fake).
- **Modelo barato**: `deepseek-chat` por default. `forced_model` configurável por env do CI.
- **Budget**: pipeline E2E inteira deve custar < $0.05 por execução completa. Documentar custo na primeira execução.
- **Determinismo**: temperatures baixas (0.1) onde possível; não testar texto literal — testar invariantes ("contém X", "len ≥ Y", "audit type Z presente").
- **Limpeza**: cada teste usa `tmp_path` próprio; nada compartilhado.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest -m e2e deilebot/tests/e2e/ -v` passa 100% (10/10 cenários) |
| AC-2 | Custo total < $0.05 (medido pelo `usage_repository` do DEILE — gerar relatório no fim do run) |
| AC-3 | Tempo total < 5 minutos em CI |
| AC-4 | Cobertura combinada (unit + e2e) ≥ 88% das linhas em `deilebot/foundation/` |
| AC-5 | Nenhum teste flakey em 3 execuções consecutivas |

## Pontos de atenção

- **`real_agent` precisa rodar `bootstrap_providers(router=get_model_router())`** — cuidado com a ordem (CLAUDE.md tem nota sobre isso).
- **Limpeza de processo**: `OneshotSubprocessAgentBridge` testado em E2E-1 alternativo deve garantir que o subprocess termina (sem zumbis).
- **Concorrência (E2E-9)** é onde bugs sutis aparecem. Se falhar intermitente, **não** acrescentar `sleep` — investigar.
- **Documentar custo aproximado por cenário** num comentário no topo de cada `test_*.py`.

## Estimativa de esforço

2 dias para implementar + estabilizar.
