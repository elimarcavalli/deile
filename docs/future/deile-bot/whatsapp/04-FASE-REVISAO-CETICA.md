# Fase Revisão Cética — WhatsApp

## Roteiro

1. **Leitura** (90min): 00-PLAN, fases 01-02, código.
2. **Auditoria foundation**: as extensões `ConversationWindow`/`TemplateMessage`/`OutboundIntent` se encaixaram limpas, ou viraram acoplamento WhatsApp na foundation? (Verificar imports.)
3. **Adversariais**:
   - ADV-W1: webhook recebe payload corrompido → 400, não 500.
   - ADV-W2: token Meta expira em vôo → erro tratado, audit registrado.
   - ADV-W3: usuário fora da janela manda 50 msgs em 1 min — pipeline ainda processa inbound (não é o bot que está fora da janela).
   - ADV-W4: template não-aprovado pelo Meta → erro `132001` mapeado para mensagem clara.
   - ADV-W5: tool `send_template_message` chamada com template inexistente no catálogo → falha clara.
   - ADV-W6: rate limit do tier estourado → `RateLimited`, DLQ ou backoff.
4. **Compliance**:
   - [ ] Política de privacidade pública vinculada?
   - [ ] Opt-in documentado?
   - [ ] Termos WhatsApp Business respeitados (sem promotional spam fora de janela)?
   - [ ] Templates registrados na conta Meta antes do uso?
5. **Perguntas**:
   - Custo médio mensal projetado por usuário ativo?
   - Como escalar de tier 1 → 2 → 3?
   - Estratégia de re-engagement: template auto vs decisão humana?
   - Plano para failover de token (rotação periódica)?

## Saída

`04-REVISAO-RESULTADOS.md`.

## Estimativa

1 dia.
