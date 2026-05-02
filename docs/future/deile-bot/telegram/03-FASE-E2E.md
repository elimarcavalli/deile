# Fase E2E — Telegram

> Bateria contra bot Telegram de teste (criar via @BotFather; token em `.env.test`).

## Cenários

1. **TE2E-1** — `/deile diga oi` em chat privado → resposta em < 30s.
2. **TE2E-2** — `/deile escreva um texto longo` → streaming visível via edits.
3. **TE2E-3** — Bot adicionado a grupo; mensionado → responde; mensagens normais → ignora (ou conforme intent).
4. **TE2E-4** — `/help` lista BotCommands.
5. **TE2E-5** — `/capabilities` produz embed (texto+lista).
6. **TE2E-6** — Inline keyboard de seleção: `/persona override` mostra botões; clicar dispara callback.
7. **TE2E-7** — Topic em supergrupo herda contexto do parent.
8. **TE2E-8** — `/forget` (owner) apaga histórico no `ConversationStore`.
9. **TE2E-9** — Reaction (Bot API 7+) registrada como engajamento.
10. **TE2E-10** — Webhook mode: enviar via webhook em vez de polling, verificar paridade.

## Critérios

| # | Verificar |
|---|---|
| AC-1 | 10/10 cenários passam |
| AC-2 | Custo < $0.30 |
| AC-3 | Tempo total < 45min (com manualidade aceitável) |

## Estimativa

1 dia.
