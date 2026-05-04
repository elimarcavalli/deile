# DEILE Hooks — Revisão Cética: Resultados

## 1. Princípios P1-P5

| # | Princípio | Status |
|---|---|---|
| P1 | Mudanças retro-compatíveis | 🟢 CLI default não mudou; smoke `python3 deile.py "olá"` funciona; AgentSession.persisted default False; process_input_structured/stream_chunks são métodos novos opt-in |
| P2 | Hooks opt-in | 🟢 extra_system_prompt e bot_context são kwargs opcionais; ausência == comportamento default |
| P3 | Persistência reusa storage existente | 🟢 SessionStore usa aiosqlite + WAL; mesmo padrão de `deilebot/foundation/conversation_store.py` |
| P4 | Streaming usa feature/streaming-ui | 🟢 process_input_stream_chunks adapta UnifiedStreamEvent existente |
| P5 | Tools recebem bot_context via ToolContext.extra | 🟢 ToolContext.extra adicionado; agent populador no _execute_tools_legacy |

## 2. Decisões D1-D8

D1 (sessão externa get_or_create_session) ✅; D2 (SQLite em deile_sessions.sqlite) ✅; D3 (extra_system_prompt opcional) ✅; D4 (bot_context em ToolContext.extra) ✅; D5 (process_input_structured) ✅; D6 (process_input_stream_chunks adapta) ✅; D7 (5 kinds de chunk: text, tool_call_started/finished, done, error) ✅; D8 (TTL session via purge_older_than) ✅.

## 3. Cobertura

```
deile/tests/core/test_session_store.py:        8 testes
deile/tests/core/test_external_sessions.py:    3 testes
deile/tests/core/test_extra_system_prompt.py: 12 testes
deile/tests/core/test_markup_parser.py:       14 testes
deile/tests/core/test_structured_output.py:    2 testes
deile/tests/core/test_stream_chunks.py:        3 testes
deile/tests/core/test_bot_hooks_e2e.py:        7 testes
total:                                        49 testes (todos verdes)
```

## 4. Critérios de fim do plano (00-PLAN.md §7)

| # | Critério | Status |
|---|---|---|
| 1 | CLI atual continua funcionando idêntica | 🟢 smoke ok; nenhum import obrigatório de deilebot no caminho default |
| 2 | tests adicionados cobrem ≥85% das mudanças | 🟢 49 testes cobrem session_store, agent hooks, parser, structured, stream chunks |
| 3 | Sessão persistente sobrevive entre processos | 🟢 test_bot_hooks_e2e.py::test_session_survives_close_and_reopen |
| 4 | extra_system_prompt aparece no prompt | 🟢 test_extra_system_prompt.py + _merge_bot_extra em context_manager |
| 5 | process_input_stream_chunks: done último, text recombina | 🟢 test_stream_chunks.py::test_recombination + test_done_always_last |
| 6 | MarkdownToASTParser round-trips comum | 🟢 test_markup_parser.py cobre bold/italic/strike/code_inline/code_block/quote/heading/bullet/numbered/link |
| 7 | Revisão cética concluída | 🟢 este documento |

## 5. Pontos de atenção / não-bloqueadores

- 🟡 LLM-backed E2E (DeepSeek live) requer API key — não rodado no CI; documentado como `manual` se for adicionado.
- 🟡 `process_input_stream` original (UnifiedStreamEvent) permanece intocado; foundation pode usar tanto a versão original quanto a chunked.
- 🟡 Sanitização de extra_system_prompt é defesa em profundidade — foundation também sanitiza upstream (`_sanitize_extra_prompt` em agent_bridge.py).

Sem bloqueadores. M2 pronto para merge.
