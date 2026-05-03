# Fase E2E — Testes ponta a ponta dos hooks DEILE

## Pré-requisitos

- Fases 1, 2, 3 mergeadas.
- `DEEPSEEK_API_KEY` no ambiente do CI.

## Cenários

### E2E-D1. Sessão persistente sobrevive a restart

1. Bootstrap agente A. `await agent_a.get_or_create_session("bot_session_alice", persisted=True)`.
2. `await agent_a.process_input("meu nome é Alice", session_id="bot_session_alice")`.
3. `await agent_a.shutdown()`.
4. Bootstrap agente B (processo conceitual novo, mesmo arquivo SQLite). `await agent_b.get_or_create_session("bot_session_alice", persisted=True)`.
5. `await agent_b.process_input("qual é o meu nome?", session_id="bot_session_alice")` deve mencionar "Alice".

### E2E-D2. `extra_system_prompt` chega ao provider

1. Mock do `ModelRouter` que captura `messages` enviados.
2. `await agent.process_input("oi", extra_system_prompt="<bot_capabilities>tool_x: faz X</bot_capabilities>")`.
3. Verificar que mensagem `system` contém o bloco.

### E2E-D3. `bot_context` chega à tool

1. Tool de teste `RecordContextTool` que copia `ctx.extra["bot_context"]` para uma lista global.
2. `await agent.process_input("use record_context tool", bot_context={"provider":"discord","scope":"DM"})`.
3. `RecordContextTool.captured == [{"provider":"discord","scope":"DM"}]`.

### E2E-D4. `process_input_structured` produz AST estável

1. `await agent.process_input("explique python em 3 bullets")`.
2. Resposta tem ≥ 3 spans `BULLET`.
3. `text` recombinado dos spans bate com `response.text`.

### E2E-D5. `process_input_stream` chunk integrity

1. Iterar `process_input_stream("liste 5 frutas")`.
2. Recombinar `text` chunks.
3. Comparar com `done.text` — devem ser iguais byte a byte.
4. `done` é o último chunk emitido. Sempre.

### E2E-D6. CLI sem regressão

Smoke completo:
- `python3 deile.py "olá"` retorna texto não-vazio.
- `python3 deile.py --model deepseek:deepseek-chat "qual é 2+2?"` responde "4" (ou contém).
- Modo interativo: digitar 1 mensagem, receber resposta, sair limpo.

### E2E-D7. Streaming + sessão persistente

Combinação dos hooks: stream com `extra_system_prompt` + `session_id` persistente. Verificar que `done` carrega `model_used` correto e que o agente B (cenário D1) lembra de "Alice" mesmo com chamada via stream.

## Critérios de aceitação

| # | Como verificar |
|---|---|
| AC-1 | `pytest -m e2e deile/tests/core/test_bot_hooks_e2e.py` passa 100% |
| AC-2 | Custo total < $0.05 |
| AC-3 | Tempo total < 4 minutos |
| AC-4 | CLI smoke sem regressão |

## Estimativa

1 dia.
