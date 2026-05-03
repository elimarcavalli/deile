# 00 — Plano completo: mudanças no DEILE para suportar bots

## 1. Motivação

`deile_bot/foundation/agent_bridge.py:InProcessAgentBridge` precisa que o `DeileAgent` aceite três coisas que hoje não são contratos públicos:

- Sessão com id arbitrário, persistente entre processos (para sobreviver a restart do bot sem perder contexto do usuário).
- Bloco extra de system prompt por invocation (para injetar capacidades do bot dinamicamente).
- Saída estruturada (AST de markup) e/ou streaming chunk-a-chunk (para o adapter renderizar por provider e atualizar mensagem progressivamente).

Sem essas mudanças o bot funciona, mas **degradado**: cada turno do usuário é uma sessão fresh, capabilities ficam hardcoded no system prompt do adapter, e a UX do Discord não pode usar `message.edit` para mostrar resposta sendo gerada.

## 2. Princípios

| # | Princípio | Por quê |
|---|---|---|
| P1 | **Mudanças retro-compatíveis.** A CLI atual continua funcionando sem nenhuma mudança no comportamento default. | Não quebra usuários atuais. |
| P2 | **Hooks são opt-in.** Se o consumidor não passar `extra_system_prompt`, nada muda. | Acoplamento mínimo. |
| P3 | **Persistência reusa o que já existe** (`deile/storage/`, `deile/memory/episodic_memory.py`). Sessão é metadado fino, não estado novo. | Evitar criar mais um silo de storage. |
| P4 | **Streaming usa o `feature/streaming-ui`** já em desenvolvimento. Não duplicar. | Convergência. |
| P5 | **Tools recebem `bot_context` via `ToolContext.extra`** quando invocadas via bot. Tools puras (CLI) não veem nada. | Sem vazar conceito de "bot" para dentro de tools que não precisam dele. |

## 3. Decisões

| # | Decisão | Motivo |
|---|---|---|
| D1 | Sessão externa = `agent.get_or_create_session(session_id, working_directory, persisted=True)`. Persistência opt-in. | Default do CLI continua transient; bot opta por persistente. |
| D2 | Estado de sessão persistente vai em SQLite (mesmo arquivo da memória episódica). Schema: `(session_id, created_at, last_used_at, working_directory, context_data_json)`. | Reusa infra de `deile/storage/`. |
| D3 | `extra_system_prompt` é parâmetro opcional de `agent.process_input(...)`. Concatena ao system prompt da persona resolvida na chamada. | Mais simples que pluginar via hook event. |
| D4 | `bot_context` é `Mapping` opcional em `process_input`. É repassado em `ToolContext.extra["bot_context"]` para tools. | Tools que precisam (ex.: `send_dm` inventada pelo bot) acessam; tools que não precisam ignoram. |
| D5 | Saída AST = método novo `agent.process_input_structured(...)` que retorna `StructuredResponse(text, markup_ast)`. AST é construída a partir do texto pelo `MarkdownToASTParser` shared (vive em `deile/ui/markup.py`, mais tarde reaproveitado pela foundation do bot). | Converge "renderização rica" da CLI com formatação de bot. |
| D6 | Streaming = método novo `agent.process_input_stream(...)` que devolve `AsyncIterator[StreamChunk]`. Já existe em parte na branch `feature/streaming-ui`; este plano formaliza o contrato. | Reuso. |
| D7 | Cada chunk do stream tem tipo: `text` \| `markup_span` \| `tool_call_started` \| `tool_call_finished` \| `done`. | Adapter pode escolher exibir/animar tool_calls ou só texto. |
| D8 | Sessões persistentes têm TTL configurável (default 30 dias) + comando admin para purga. | Privacidade. |

## 4. Escopo (in/out)

**In:**

- `agent.get_or_create_session` (nova).
- Persistência de sessão em SQLite.
- `agent.process_input(extra_system_prompt=, bot_context=)` (extensão retrocompatível).
- `agent.process_input_structured(...)`.
- `agent.process_input_stream(...)` (formalização do que já está em `feature/streaming-ui`).
- `MarkdownToASTParser` em `deile/ui/markup.py`.
- Testes unit dos novos métodos.

**Out:**

- Tools novas para uso do bot (ex.: `send_dm`) — vivem em `deile_bot/foundation/tools/` ou no plano discord; não aqui.
- UI da CLI mudando — esta entrega não muda nenhum byte da CLI default.

## 5. Riscos

| Risco | Mitigação |
|---|---|
| Persistir `context_data_json` que cresce sem bound | Limite de tamanho por sessão (ex.: 256KB); rotação de chaves antigas |
| `extra_system_prompt` ser usado para prompt injection (usuário do bot manda `</system>` nas mensagens, foundation devolve para cá) | A sanitização vive na foundation (não aqui). Aqui só anexamos texto. |
| Streaming na CLI travar quando consumido pelo bridge | Testes paralelos: CLI segue funcionando após `process_input_stream` ser usado pelo bridge no mesmo processo |
| Conflito com `feature/streaming-ui` em desenvolvimento | Coordenar merge order: streaming-ui primeiro, depois este plano |

## 6. Mapa de fases

| Fase | Entregáveis | Bloqueia |
|---|---|---|
| 01 | Sessão externa persistente, schema, get_or_create_session | fase 02, 03 |
| 02 | extra_system_prompt + bot_context em process_input + repasse no ToolContext | bridge in-process |
| 03 | process_input_structured + process_input_stream + MarkdownToASTParser | streaming UX no Discord |
| E2E | Testes que provam: sessão sobrevive restart, extra_system_prompt aparece, streaming entrega chunks na ordem | revisão |
| Revisão | Roteiro cético | release |

## 7. Critérios de "feito" do plano inteiro

1. CLI atual continua funcionando idêntica em todos os fluxos (smoke `python3 deile.py "olá"`, `python3 deile.py --model X "..."`, interativo).
2. `tests/` adicionados cobrem ≥85% das mudanças.
3. Sessão `bot_session_X` persiste entre dois processos consecutivos (E2E).
4. `extra_system_prompt` aparece no prompt enviado ao provider LLM (verificável via mock do provider).
5. `process_input_stream` entrega `done` como último chunk; `text` recombinado bate com `process_input(...)` (mesma seed).
6. `MarkdownToASTParser` round-trips texto markdown comum (bold/italic/code/quote/codeblock/heading/list/link).
7. Revisão cética concluída.
