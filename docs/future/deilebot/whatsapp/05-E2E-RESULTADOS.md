# Fase E2E — resultados

> **Estado: PENDENTE — aguardando operador humano com credenciais Meta Business.**
>
> Os 10 cenários listados aqui só podem ser executados contra um número
> WhatsApp Business sandbox real (verificação de negócio + templates aprovados).
> O agente não tem acesso às credenciais e não pode rodar esta bateria.
>
> Runbook completo (provisionamento, webhook, templates, validação):
> [`elimarcavalli/deilebot:docs/whatsapp/SETUP.md`](https://github.com/elimarcavalli/deilebot/blob/main/docs/whatsapp/SETUP.md)
> (mergeada via PR de Phase 2 — issue #157).

## Pré-requisitos antes de rodar

1. Conta Meta Business com WABA criada
2. Pelo menos 1 número WhatsApp Business (sandbox serve)
3. Token de System User permanente
4. Webhook HTTPS público apontando para o bot
5. Pelo menos 2 templates aprovados pela Meta:
   - Um `utility` sem parâmetros (ex.: `hello_world`)
   - Um `utility` com 2 parâmetros body (ex.: `appointment_reminder`)
6. `config/whatsapp_templates.yaml` espelhando os templates aprovados
7. Bot rodando com control plane HTTP exposto
8. DEILE com `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` configurados

## Bateria

Spec dos cenários: [`03-FASE-E2E.md`](03-FASE-E2E.md). Operator preenche a coluna **Resultado** após cada execução.

| # | Cenário | Critério de aprovação | Resultado | Data | Notas |
|---|---|---|---|---|---|
| 1 | Inbound texto dentro da janela → reply texto livre | Mensagem visível no cliente WhatsApp | ⏳ pendente | — | — |
| 2 | Inbound imagem → adapter recebe `Attachment(IMAGE)` | Tool vê mime + url | ⏳ pendente | — | — |
| 3 | Reply Button click → `interactive.button_reply` parsed → `env.text == title`, `env.raw["interactive"]["id"] == callback_data` | Mensagem com botão enviada via tool, clique processado | ⏳ pendente | — | — |
| 4 | List selection → `interactive.list_reply` parsed identicamente | Idem | ⏳ pendente | — | — |
| 5 | Texto livre fora da janela 24h → adapter rejeita (ou pipeline roteia para template) | Sem envio silencioso | ⏳ pendente | — | — |
| 6 | Send template (utility, sem params) | Mensagem chega; `bot_whatsapp_conversations_total{category=utility,status=ok}` incrementa | ⏳ pendente | — | — |
| 7 | Send template (utility, 2 params body) | Variáveis substituídas corretamente | ⏳ pendente | — | — |
| 8 | Template name não aprovado → `BOT_UPSTREAM` (132001) chega ao operador | Tool retorna erro, sem cobrança | ⏳ pendente | — | — |
| 9 | Param-count mismatch → `ProviderError` antes da chamada HTTP | Erro nomeia o template + contagem esperada | ⏳ pendente | — | — |
| 10 | Send template (marketing, após business verification) | Meta aceita; `category=marketing` incrementa | ⏳ pendente | — | — |

## Como reportar resultados

Para cada cenário executado:

1. Rodar e observar.
2. Marcar **Resultado** como ✅ (passou), ❌ (falhou) ou ⚠️ (parcial).
3. Preencher **Data** com YYYY-MM-DD.
4. Em **Notas**, registrar:
   - Output relevante do bot log (sem token!)
   - Snapshot da métrica `bot_whatsapp_conversations_total`
   - Qualquer divergência do critério

## Critério de fechamento da Fase E2E

A Fase E2E só está concluída quando:

- [ ] Todos os 10 cenários estão ✅ ou ⚠️ (com nota explicativa de por que ⚠️ é aceitável).
- [ ] Cenário #5 está ✅ (proteção de janela é o controle de gasto crítico).
- [ ] Cenário #6 está ✅ (template send é o caminho feliz primário).
- [ ] Snapshot final da métrica anexado a este doc.

Falhas em #1–#4 são bloqueantes (capacidade básica). Falha em #8/#9 indica que algo no catálogo ou no adapter está silenciando erros — também bloqueante.
