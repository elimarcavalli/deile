# Fase Revisão Cética — Telegram

## Roteiro

1. **Leitura**: 00-PLAN, fases 01-02, código (90min).
2. **Auditoria de paridade com Discord**: features equivalentes funcionam? Há regressões?
3. **Adversariais**:
   - ADV-T1: bot adicionado a grupo de 10000 membros → não trava.
   - ADV-T2: usuário envia 100 msgs em 10s → rate limit.
   - ADV-T3: MarkdownV2 com caracteres especiais não escapados → escape funciona.
   - ADV-T4: Update sem `effective_user` → trata gracefully.
   - ADV-T5: Webhook recebe POST inválido → 400, não 500.
   - ADV-T6: BotCommands de outro bot vazam (`set_my_commands` por escopo) → escopo correto.
4. **Perguntas**:
   - Polling vs webhook: qual é o default em produção?
   - Como migrar de polling para webhook sem downtime?
   - Topics têm o mesmo TTL de retenção?

## Saída

`04-REVISAO-RESULTADOS.md`.

## Estimativa

0.5 dia revisor + 0.5 dia réplica.
