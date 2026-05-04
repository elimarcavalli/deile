# Fase E2E — Messenger + Instagram

## Cenários (Messenger)

1. **ME2E-1** — Inbound DM em Messenger → bot responde via DEILE.
2. **ME2E-2** — Quick replies enviados → usuário clica → callback chega.
3. **ME2E-3** — Postback de menu → handler dispara.
4. **ME2E-4** — Janela fechada + `message_tag=ACCOUNT_UPDATE` → mensagem entregue.
5. **ME2E-5** — Reaction enviada via tool.

## Cenários (Instagram)

6. **IE2E-1** — Inbound DM em Instagram → bot responde.
7. **IE2E-2** — Story reply normalizada como `ReplyContext`.
8. **IE2E-3** — Mensagem > 1000 chars splitada em 2.
9. **IE2E-4** — Tool `reply_to_story` envia resposta.
10. **IE2E-5** — Janela fechada + human_agent tag (se aplicável).

## Critérios

| # | Verificar |
|---|---|
| AC-1 | 10/10 cenários passam |
| AC-2 | Custo Meta < $0.20 |
| AC-3 | Tempo total < 90min (com manual) |

## Estimativa

2 dias.
