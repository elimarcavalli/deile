# Fase E2E — WhatsApp

> Bateria contra número WhatsApp Business de teste. Custa real (em conversações).

## Cenários

1. **WE2E-1** — Usuário inicia conversa via /msg; bot responde; conversação `service` registrada.
2. **WE2E-2** — Resposta longa (>4096 chars) é split em N mensagens.
3. **WE2E-3** — Após 24h, sistema envia template `re_engagement_default_pt_br`; usuário responde; nova conversação `service` aberta.
4. **WE2E-4** — Mídia: usuário envia foto; bot responde com texto + foto.
5. **WE2E-5** — Interactive List: bot envia menu; usuário escolhe; callback dispara handler.
6. **WE2E-6** — Tool `send_template_message` chamada pelo agente DEILE → mensagem entregue.
7. **WE2E-7** — Janela fechada + sem template configurado → DLQ + audit; sem crash.
8. **WE2E-8** — Métrica `bot_whatsapp_conversations_total{type}` incrementa corretamente.
9. **WE2E-9** — Webhook handshake re-verificação (Meta às vezes re-verifica) funciona.
10. **WE2E-10** — Failover: token expira → erro 401 → audit + alerta; bot não trava.

## Critérios

| # | Verificar |
|---|---|
| AC-1 | 10/10 cenários passam |
| AC-2 | Custo Meta < $1 |
| AC-3 | Tempo total < 90min (com manual) |

## Estimativa

2 dias.
