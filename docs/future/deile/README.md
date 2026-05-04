# `docs/future/deile/` — mudanças no agente DEILE para integração com bots

> Tudo o que `deilebot/` precisa que o `deile/` (agente) entregue como hook ou contrato. **Não** mudanças cosméticas; só hooks que destravam features do bot.

## Documentos

| # | Arquivo | Conteúdo |
|---|---|---|
| — | [`README.md`](README.md) | este arquivo |
| 00 | [`00-PLAN.md`](00-PLAN.md) | plano completo: motivação, decisões, escopo, riscos |
| 01 | [`01-FASE-1-sessoes-externas-e-persistentes.md`](01-FASE-1-sessoes-externas-e-persistentes.md) | API para sessões com id externo (`bot_session_<bot_user_id>`) e snapshot/resume |
| 02 | [`02-FASE-2-extra-system-prompt-e-context-channel.md`](02-FASE-2-extra-system-prompt-e-context-channel.md) | injeção de `extra_system_prompt` (capabilities) e `bot_context` (provider, scope, channel) acessíveis para tools |
| 03 | [`03-FASE-3-output-formatters-e-streaming-callback.md`](03-FASE-3-output-formatters-e-streaming-callback.md) | hook de saída para markup-aware rendering + callback de streaming chunk-a-chunk |
| 04 | [`04-FASE-E2E.md`](04-FASE-E2E.md) | E2E que prova hooks com bot fake invocando agente real |
| 05 | [`05-FASE-REVISAO-CETICA.md`](05-FASE-REVISAO-CETICA.md) | revisão cética |

## Estado

| Item | Estado |
|---|---|
| Plano escrito | ✅ |
| Implementado | ❌ |
| Testado E2E | ❌ |
| Revisado | ❌ |

## Resumo

O DEILE atual é uma CLI. O bot é outro modo de uso. Para o bridge in-process funcionar, o agente precisa de **três hooks pequenos mas obrigatórios**:

1. **Sessões externas**: hoje `agent.create_session(session_id="...")` aceita string mas não tem persistência cross-process. Bot precisa que sessões com ids estáveis (`bot_session_<bot_user_id>`) sobrevivam restart.
2. **Extra system prompt**: bot injeta um bloco `<bot_capabilities>` por chamada — o agente precisa expor um ponto de injeção que não exija reescrever a persona.
3. **Output formatters opcionais + streaming callback**: hoje a resposta sai como string. Bot precisa de markup AST opcional (para renderizar por provider) e de callback chunk-a-chunk para `message.edit` progressivo.

Nenhum dos três quebra a CLI. Todos são opt-in.
