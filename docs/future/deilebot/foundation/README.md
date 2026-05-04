# `deilebot/foundation/` — plano da camada provider-agnóstica

> Tudo o que **não depende** de Discord, Telegram, WhatsApp ou Meta vive aqui. Esta é a única camada que evolui quando uma nova capacidade transversal nasce.

## Documentos deste plano

| # | Arquivo | Conteúdo |
|---|---|---|
| — | [`README.md`](README.md) | este arquivo (navegação + contrato) |
| 00 | [`00-PLAN.md`](00-PLAN.md) | plano completo: motivação, decisões, escopo, riscos, dependências |
| 01 | [`01-FASE-1-pacote-base-e-modelo-de-dominio.md`](01-FASE-1-pacote-base-e-modelo-de-dominio.md) | esqueleto do pacote, DTOs, `MarkupAST`, settings, exceptions |
| 02 | [`02-FASE-2-servicos-core.md`](02-FASE-2-servicos-core.md) | identity, permissions, rate limit, conversation store (SQLite), audit, intent |
| 03 | [`03-FASE-3-bridge-e-capabilities.md`](03-FASE-3-bridge-e-capabilities.md) | `AgentBridge` (in-process + oneshot), `CapabilityCatalog`, `PersonaSelector`, `EventBus` wrap, métricas, DLQ |
| 04 | [`04-FASE-E2E.md`](04-FASE-E2E.md) | bateria E2E que prova o contrato da foundation com adapter mock e DEILE real |
| 05 | [`05-FASE-REVISAO-CETICA.md`](05-FASE-REVISAO-CETICA.md) | roteiro de revisão cética por outra pessoa |

## Estado

| Item | Estado |
|---|---|
| Plano escrito | ✅ |
| Implementado | ❌ |
| Testado E2E | ❌ |
| Revisado ceticamente | ❌ |

## Resumo de uma linha

A foundation entrega **um contrato** (`ProviderAdapter` ABC), **um modelo de domínio** (`MessageEnvelope`, `MarkupAST`, `BotUser`) e **uma pilha de serviços** (identity, permissions, rate limit, conversation store, audit, agent bridge, capability catalog, persona selector) que qualquer adapter de provider consome sem reescrever nada.
