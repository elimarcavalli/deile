# `deile-bot/discord/` — plano completo do adapter Discord

> Primeiro provider real. Substitui (com migração planejada) o `discord_bot/` legado. **Foco principal**: este plano consome integralmente a foundation + os hooks DEILE e entrega um bot Discord pronto para impressionar.

## Documentos

| # | Arquivo | Conteúdo |
|---|---|---|
| — | [`README.md`](README.md) | este arquivo |
| 00 | [`00-PLAN.md`](00-PLAN.md) | plano completo: motivação, decisões D1-D20, escopo, riscos, migração do legado |
| 01 | [`01-FASE-1-emergencia-seguranca-e-shell-do-adapter.md`](01-FASE-1-emergencia-seguranca-e-shell-do-adapter.md) | endereçar S1-S4 da auditoria + DiscordAdapter shell + slash sync + d!help real |
| 02 | [`02-FASE-2-pipeline-completa-e-tools-de-bot.md`](02-FASE-2-pipeline-completa-e-tools-de-bot.md) | normalizer, formatter, ingress/egress reais, tools `send_dm`/`get_user_profile`, persona resolution |
| 03 | [`03-FASE-3-bridge-deile-streaming-e-comandos-canonicos.md`](03-FASE-3-bridge-deile-streaming-e-comandos-canonicos.md) | bridge in-process com streaming, `/deile`, mention trigger, reaction-trigger 🤖, `/capabilities`, sessões por usuário |
| 04 | [`04-FASE-4-eventos-proativos-e-scheduler-genérico.md`](04-FASE-4-eventos-proativos-e-scheduler-genérico.md) | on_member_join, threads, daily digest, scheduler genérico (substitui scheduler_333), `/dlq`, `/forget` |
| 05 | [`05-FASE-E2E.md`](05-FASE-E2E.md) | E2E completa contra um servidor Discord de teste |
| 06 | [`06-FASE-REVISAO-CETICA.md`](06-FASE-REVISAO-CETICA.md) | revisão cética |

## Estado

| Item | Estado |
|---|---|
| Plano escrito | ✅ |
| Implementado | ❌ |
| Testado E2E | ❌ |
| Revisado | ❌ |

## Resumo

- **Fase 1** apaga o sangue: tokens hardcoded vão embora, sistema de obediência por display_name vai embora, jailbreak `set_modulo_regulador` vai embora, slash sync funciona, `d!help` real existe. Adapter shell já está plugado na foundation, mas ainda usa fluxo simples.
- **Fase 2** entrega o pipeline completo. Discord-specific normalizer e formatter funcionando, conversation store ativo, persona selector roteando. As primeiras tools de bot (`send_dm`, `get_user_profile`) existem.
- **Fase 3** pluga o agente DEILE. `/deile` slash command, `@bot` invoca o agente, reaction 🤖 em qualquer mensagem dispara o agente, streaming progressivo via `message.edit`. Sessão DEILE persiste por `bot_user_id`.
- **Fase 4** desbloqueia o reativo+proativo. `on_member_join` saudação personalizada, threads herdam contexto, daily digest, scheduler genérico configurável, comandos admin `/dlq`, `/forget`, `/sessions`.

## Migração do `discord_bot/` legado

- Preservar `memory.json` durante a fase 1 com script `migrate_memory_json_to_sqlite.py`. Documentado em [`02-FASE-2-pipeline-completa-e-tools-de-bot.md`](02-FASE-2-pipeline-completa-e-tools-de-bot.md) §X.
- `discord_bot/scheduler_333.py`, `nuke.py`, `salve_tiago.py`, `send_dm.py`, `disparar_agora.py` ficam **read-only** em `archive/discord_bot_legacy/` desde a fase 1; são removidos no merge final da fase 4.
- Tokens dos scripts legados são **rotacionados antes** de qualquer commit que ainda referencie esses arquivos. Operação humana.
