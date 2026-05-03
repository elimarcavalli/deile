# Fase Revisão Cética — Meta (Messenger + Instagram)

## Roteiro

1. **Leitura** (90min): 00-PLAN, fases 01-02, código.
2. **Auditoria de paridade**: foundation segura os 2 adapters sem hack? `_common/` realmente compartilhado? Ou virou copy-paste disfarçada?
3. **Adversariais**:
   - ADV-M1: webhook recebe evento de Page que não é a configurada → ignora.
   - ADV-M2: Page Access Token expira → rotação automática se token de longa duração; senão alerta.
   - ADV-M3: usuário envia mídia Instagram não suportada (PDF) → fallback gracioso.
   - ADV-M4: rate limit Graph API estourado → backoff + DLQ.
   - ADV-M5: App Review revoga permissão → erro 10/200 → audit + alerta operacional.
4. **Compliance**:
   - [ ] Política de privacidade pública?
   - [ ] Tags de janela usadas só nos cenários permitidos?
   - [ ] Opt-in respeitado?
5. **Perguntas**:
   - Como uma Page com 10 conversas simultâneas se comporta?
   - Estratégia para múltiplas Pages no mesmo app?
   - Se o agente quiser responder uma story, ele sabe que não pode além de 24h sem human_agent tag?

## Saída

`04-REVISAO-RESULTADOS.md`.

## Estimativa

1 dia.
