# Spike #529 — Compactação in-place no claude-worker: Relatório de Resultados

> **DESCARTÁVEL** — Código e relatório sob `infra/k8s/spikes/` são artefatos de spike,
> não de produção. Preencher esta seção após execução no pod claude-worker.

---

## Gate 0 — Pré-requisito BLOQUEANTE

### AC0 — OAuth × Compaction API (beta `compact-2026-01-12`)

> **Execução in-pod 2026-06-18** (claude-worker `claude-worker-854cf94b97-28szb`):
> harness rodado de fato (não pulou) via `python3 infra/k8s/spikes/compaction_oauth_spike.py`.
> O SDK alcançou `POST https://api.anthropic.com/v1/messages?beta=true` (egress liberado —
> caminho in-process validado), retornando **401 `authentication_error`** porque o **único
> token presente no pod é o `accessToken` OAuth EXPIRADO** em `~/.claude/credentials.json`
> (`expiresAt=1781385313868` ≈ 2026-06-13, ~4.6 dias vencido; `ANTHROPIC_AUTH_TOKEN` não-setado).
> **Este 401 é artefato de credencial vencida, NÃO refutação de AC0** — não prova que OAuth
> seja incompatível com a Compaction beta; a request foi rejeitada na autenticação, antes da
> validação do beta header. O refresh in-pod NÃO rotaciona o refresh_token (trap conhecido,
> `CLAUDE.md` §5.6) → o operador precisa rodar `python3 infra/k8s/deploy.py k8s claude-renew`
> no host e re-disparar AC0. **Veredito AC0 = PENDENTE token fresco** (não APROVADO nem REPROVADO).

| Campo | Valor (execução in-pod 2026-06-18) |
|---|---|
| Status HTTP | **401** (`authentication_error: Invalid authentication credentials`, `req_011CcAj8QUDFQ4hbjjmxbM81`) |
| Beta header aceito | indeterminado — rejeitado na autenticação, antes da validação do beta |
| Token OAuth (`ANTHROPIC_AUTH_TOKEN`) | **inválido (expirado)** — env não-setado; on-disk `accessToken` vencido ~4.6d |
| Erro (se houver) | 401 authentication_error (token OAuth expirado) |
| Ação de escalação | operador roda `k8s claude-renew` no host → re-dispara AC0 com token fresco → só então AC0 é avaliável |

**Como rodar:**
```bash
cd /home/claude/work/<task_id>/repo
export ANTHROPIC_AUTH_TOKEN="$(cat ~/.claude/credentials.json | python3 -c "import sys,json; d=json.load(sys.stdin); print(d.get('claudeAiOauth',d).get('accessToken',''))")"
python3 infra/k8s/spikes/compaction_oauth_spike.py
# ou via pytest:
python3 -m pytest infra/k8s/spikes/test_compaction_oauth.py::test_ac0_compaction_beta_with_oauth_returns_200 -v -m integration -p no:cov
```

**Resultado AC0:** [ ] APROVADO / [ ] REPROVADO / [x] **PENDENTE — token OAuth expirado no pod (2026-06-18); requer `k8s claude-renew` no host e re-execução**

Se REPROVADO:
- **401/403** → Escalar ao autor: OAuth não é aceito pela API Compaction beta.
  Decisão: migrar para API key dedicada (roadmap `#529`) ou abrir exceção OAuth via suporte Anthropic.
- **400** → Verificar valor atual do beta header em https://docs.anthropic.com/en/api/beta-features
  e atualizar `COMPACTION_BETA` em `compaction_oauth_spike.py`.

---

### AC0b — Refresh do token OAuth mid-session

| Campo | Valor |
|---|---|
| SDK renova token nativamente | sim/não |
| refresh_oauth_token() funciona | sim/não |
| Sessão sobreviveu com expiresAt vencido | sim/não |
| Modo de morte detectado | ??? |

**Como rodar:**
```bash
python3 -m pytest infra/k8s/spikes/test_compaction_oauth.py::test_ac0b_session_survives_token_expiry_in_short_run -v -m integration -p no:cov
```

**Resultado AC0b:** [ ] APROVADO / [ ] REPROVADO / [ ] SDK faz refresh nativamente

Se REPROVADO → Ação: implementar renovação in-process no caminho SDK (ver roadmap abaixo).

---

> **Os ACs abaixo só são avaliados se AC0 e AC0b estiverem APROVADOS.**

---

## ACs do Spike (preencher após replay_pr527_session.py com 40 rounds)

### AC1 — Custo

| Métrica | Baseline (fresh) | Com compaction | Resultado |
|---|---|---|---|
| Custo total USD | ??? | ??? | ratio=??? (≤70%?) |
| N compactions | — | ??? | — |
| Custo overhead compaction | — | ??? | incluído |

**Como rodar:**
```bash
python3 infra/k8s/spikes/replay_pr527_session.py --rounds 40
```

**Resultado AC1:** [ ] APROVADO (ratio ≤ 70%) / [ ] REPROVADO / [ ] N/A (AC0 falhou)

---

### AC2 — Continuidade do checkpoint

| Verificação | Resultado |
|---|---|
| Itens do checkpoint antes da compaction | ??? |
| Itens perdidos após compaction | 0? |
| Diff = 0 perdas | sim/não |

**Como rodar:**
```bash
python3 -m pytest infra/k8s/spikes/test_checkpoint_continuity.py::test_ac2_checkpoint_survives_compaction -v -m integration -p no:cov
```

**Resultado AC2:** [ ] APROVADO (0% perda) / [ ] REPROVADO / [ ] N/A

---

### AC2b — Equivalência de resultado

| Verificação | Resultado |
|---|---|
| Artefato final (sessão compactada) | ??? |
| Itens de escopo omitidos vs fresh | ??? |
| Checks que o fresh passa / compaction falha | 0? |

**Resultado AC2b:** [ ] APROVADO / [ ] REPROVADO / [ ] N/A

---

### AC3 — Sobrevivência ao gate de contexto

| Verificação | Resultado |
|---|---|
| Rounds completados | ??? / 40 |
| Promoções-a-fresh | 0? |
| Sessão foi a mesma do início ao fim | sim/não |

**Resultado AC3:** [ ] APROVADO (≥40 rounds, 0 promoções) / [ ] REPROVADO / [ ] N/A

---

### AC3b — Wall-clock (medido, não binário)

| Métrica | Baseline | Com compaction | Timeout atual |
|---|---|---|---|
| Wall-clock total (s) | ??? | ??? | 7200 |
| Cabe no timeout | — | sim/não | — |

**Recomendação:**
- [ ] Sessão cabe no timeout atual — compaction suficiente.
- [ ] Recomendar elevar `DEILE_CLAUDE_WORKER_TASK_TIMEOUT_S` para ???s.
- [ ] Implementar heartbeat/streaming no caminho SDK.

---

### AC4 — Trigger determinístico

| Verificação | Resultado |
|---|---|
| Compaction disparou em 80% ± 5pp | sim/não |
| Fração medida na primeira compaction | ???% |
| Eventos fora da faixa [75%, 85%] | ??? |
| SDK expôs usage per-turn | sim/não |

**Resultado AC4:** [ ] APROVADO / [ ] REPROVADO (trigger errado) / [ ] N/A

---

## Lacunas Arquiteturais — Resultados do Spike

| Lacuna | Resultado | Ação |
|---|---|---|
| In-place exige SDK in-process | confirmado/refutado | ver roadmap |
| Refresh OAuth mid-session | delegado SDK / manual / quebrado | item roadmap |
| Timeout wall-clock | cabe/não cabe | AC3b recomendação |
| Infra JSONL acoplada ao `claude -p` | confirmado — SDK não emite JSONL | item crítico roadmap |

---

## Roadmap pós-spike (a decidir com base nos números acima)

- [ ] **Decisão de design:** "substituir `claude -p`" vs "SDK só para sessões longas"
- [ ] **Migração OAuth→API key** *(só se AC0 falhar)*
- [ ] **Refresh OAuth in-process em produção** *(só se AC0b mostrar necessidade)*
- [ ] **Tratamento do timeout wall-clock** *(baseado em AC3b)*
- [ ] **Reconciliar infra JSONL com caminho SDK** — escolher: (a) emitir JSONL equivalente, ou (b) adaptar subsistemas
- [ ] **Implementação de produção** + testes em `deile/tests/infrastructure/test_claude_worker_server.py`

---

## Condição de Saída

- **Spike aprovado** = AC0 + AC0b + AC1 + AC2 + AC2b + AC3 + AC4 verdes; AC3b documentado.
- **Spike bloqueado** = qualquer AC duro vermelho → registrar causa acima e escalar ao autor.

---

*Gerado por: auto/issue-529 | Executar no pod claude-worker para resultados reais.*
