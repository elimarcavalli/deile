# `deile-bot/meta/` — plano dos adapters Messenger e Instagram Direct

> Esboço. Os dois compartilham infra (Meta Platform, Graph API, Pages, Webhooks) então vivem juntos. Detalhamento ao iniciar execução.

## Documentos

| # | Arquivo | Conteúdo |
|---|---|---|
| — | [`README.md`](README.md) | este arquivo |
| 00 | [`00-PLAN.md`](00-PLAN.md) | plano completo (esboço) — Messenger + Instagram juntos |
| 01 | [`01-FASE-1-adapter-messenger.md`](01-FASE-1-adapter-messenger.md) | Messenger Platform: webhook, Pages, Page Access Token, postbacks, quick replies |
| 02 | [`02-FASE-2-instagram-direct.md`](02-FASE-2-instagram-direct.md) | Instagram Direct (Graph API): Business Account, mensagens, ice-breakers |
| 03 | [`03-FASE-E2E.md`](03-FASE-E2E.md) | E2E para os dois |
| 04 | [`04-FASE-REVISAO-CETICA.md`](04-FASE-REVISAO-CETICA.md) | revisão cética |

## Estado

Esboço — depois de WhatsApp ou em paralelo.

## Razão de juntar Messenger + Instagram

Mesma plataforma Meta. Mesma estrutura de webhook (subscribe `messages`, `messaging_postbacks`, etc.). Mesmo App ID. Mesma rotação de Page Access Token. Tools de bot largamente sobrepostas. Faz sentido um plano único com 2 adapters separados que reusam código auxiliar (`meta_common/`).
