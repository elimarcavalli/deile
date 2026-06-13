# Spec — DEILE-Monitor: tick determinístico (Phase A) + julgamento LLM (Phase B)

> Issue-driver: consumo desproporcional de tokens do `deile-monitor` (63M tokens/dia
> em deepseek-flash; ~3,5M tokens/tick; ~40–60 rodadas de LLM por tick para trabalho
> 100% determinístico). Diagnóstico empírico no audit log do PVC `deile-monitor-state`
> (madrugada de 2026-06-02): 116 ticks, 111 tentativas de `oauth_renew` todas falhas
> (interactive `claude auth login`, impossível headless), 48 notificações/dia
> re-anunciando as mesmas anomalias estáticas.

## Problema-raiz

O tick inteiro é determinístico (o `monitor.md` já contém todo o bash), mas é executado
por um **agente LLM em tool-loop**: o modelo relê uma persona de 39 KB e decide cada
comando bash, re-enviando o contexto crescente a cada rodada. O LLM virou um
interpretador de bash carésimo.

## Objetivo

1. **Phase A determinística** (`infra/k8s/monitor_tick.py`, Python testável, ZERO LLM):
   automatiza tudo que é mecânico — todo o ciclo de tick, V1–V7, coleta+pré-filtro de
   V8, anti-flood, fingerprint, state, emit, steer, kube DNS-first.
2. **Phase B (LLM)** — `monitor.md` enxuto, invocado SÓ quando há julgamento real e
   **recebendo os achados** da Phase A: V8 (julgamento semântico de prosa, jaccard,
   compor/abrir issue) + anomalias novas/atípicas.
3. **Corrigir o `oauth_renew` futil**: remover `claude auth login`. (Nota: o
   refresh headless `try_refresh_claude_credentials()` desta spec foi
   posteriormente REMOVIDO na issue #603 — a auth migrou para o token de ~1 ano
   do `claude setup-token`/`CLAUDE_CODE_OAUTH_TOKEN`; o vigia agora só notifica.)
4. **Intervalo de tick 600 → 1800 s**; `DEILE_MAX_TOOL_ITERATIONS=50` na Phase B.
5. **Enxugar a persona** preservando 100% da responsabilidade.

Resultado esperado: regime estacionário (mesmas anomalias por horas, sem FU novo) =
Phase A resolve tudo → **zero LLM** → ~95% de corte de tokens.

## Arquitetura

```
shell loop (manifest 55):
  while true:
    python3 /app/infra/k8s/monitor_tick.py          # Phase A — determinística, sempre
    if [ -f /state/monitor-judgment.json ]:          # só se Phase A escalou
      python3 /app/wrapper.py monitor "$(cat ...)"   # Phase B — LLM, recebe achados
      rm -f /state/monitor-judgment.json
    sleep ${DEILE_MONITOR_TICK_INTERVAL_S:-1800}
```

### Módulos (Phase A)

| Módulo | Responsabilidade |
|---|---|
| `infra/k8s/monitor_core.py` | Motor determinístico: emit (schema), state I/O, anti-flood (cooldown P0/P1/P2 + ack + 8/h), fingerprint, notificação (curl→deilebot + fallback + templates), kube-api DNS-first |
| `infra/k8s/monitor_vigias.py` | V1–V8 (detecção + curas + coleta/pré-filtro V8) |
| `infra/k8s/monitor_tick.py` | Orquestrador: ciclo de tick, kill-switch/auto-resume, steer commands, decisão Phase B, `main()` |

### Contrato de emit (NÃO QUEBRAR — `test_monitor_emit_schema.py` + `_panel_monitor.py`)

11 famílias com formato canônico exato. Parser: `_VIGIA_RE = \bV([1-7])\b`,
`_AUDIT_TS_RE`. Cada linha: ≤500 chars, sem `\n\r\t`, stdout-first + append em
`/state/monitor-audit.log` (atomic-tolerante via guard `audit_pvc_fail` 1×/tick).
`V=V<n>` obrigatório em action/vigia.skip/vigia.fix. `flood_cap` ≤1×/(kind,tick).
O `monitor.md` enxuto MANTÉM o `_emit() {` bash + ≥10 usos + exemplos (Phase B ainda
emite v8.create/v8.skip/notify/command); o emitter Python produz formato byte-idêntico
(novo teste `test_monitor_tick_emit.py` valida equivalência).

### Renovação OAuth real (substitui `claude auth login`)

> **Obsoleto desde a issue #603.** Esta seção descrevia o refresh headless via
> `_claude_creds_refresh.try_refresh_claude_credentials` (lia creds in-pod e
> patchava o Secret `claude-credentials`). Com a migração para o token de ~1 ano
> do `claude setup-token` (env `CLAUDE_CODE_OAUTH_TOKEN`), **não há mais refresh
> headless**: o módulo `_claude_creds_refresh` foi removido e o vigia de OAuth
> apenas notifica o Humano para rodar `deploy.py k8s claude-setup-token` quando o
> token de fato expira. O **RBAC do manifest 55** (`secrets [get,patch,create]`
> em `claude-credentials` + `deployments [get]` em `claude-worker`) é mantido
> para updates pontuais do Secret via setup-token.

## Fronteira Phase A (determinística) vs Phase B (LLM)

Phase A faz **detecção + notificação** de TODAS as vigias (as notificações já são
templated hoje — nenhuma perda). Phase B é chamada SÓ para:
- **V8 FU**: candidatos que sobreviveram aos filtros determinísticos de Phase A
  (bot_author, code_block, fingerprint_seen, caps) → julgamento semântico (promessa-em-
  prosa? jaccard vs issues abertas?) + compor título/corpo + abrir issue.
- **Anomalia novel/atípica**: tipo que não casa nenhum padrão conhecido (escape hatch).

Se não houver candidato V8 nem novel anomaly → Phase B não roda. Esse é o caso comum.

## Checklist de completude (derivado do inventário de 54 responsabilidades)

### Ciclo de tick
- [ ] Step 1: load state + reset flags por-tick (PVC_FAIL/FLOOD_CAP_NOTIFY/FLOOD_CAP_FU) + TICK_START
- [ ] Step 2: kill-switch (`/state/monitor-pause`) + auto-resume por `paused_until`
- [ ] Step 3-4: dispatch vigias + fingerprint diff vs known_anomalies + ACTIONS/NOTIFICATIONS/SKIPPED
- [ ] Step 5: persist state + emit `monitor.tick #N done in Xs: actions=A notify=N skipped=[...] anomalias=K`
- [ ] Step 6: exit (sem sleep)

### Vigias
- [ ] V1: TTL OAuth (<1800s) → renew REAL; fingerprint `oauth_expired_<pod>`
- [ ] V1b: scan logs pipeline `WORKER_AUTH_EXPIRED` → renew REAL
- [ ] V2: pods Failed/Error/OOMKilled/CrashLoop; delete Job-pods BackoffLimitExceeded >1h; ≥5 erros → notify; crashloop pipeline/worker >3 restarts → notify
- [ ] V3: issues órfãs (`em_revisao|em_implementacao|em_pr`, ≥12h) → notify (renotify 6h)
- [ ] V4: PRs `auto/*` + attempt N/3 em comments (24h) → notify
- [ ] V5: `aguardando_stakeholder` ≥4h → notify (renotify 4h)
- [ ] V6: Jobs BackoffLimitExceeded >30min → notify; `claude-credentials-renew` → P0
- [ ] V7: pipeline pod não Running/Ready ou >3 restarts → notify P0
- [ ] V8: coletar issues fechadas + PRs mergeadas 24h; regex FU; pré-filtros determinísticos (bot/code_block/fingerprint/caps); escalar sobreviventes → Phase B

### Anti-flood / notificação
- [ ] 8 notify/hora (reset hour_slot UTC) → `flood_cap kind=notify` 1×/tick
- [ ] cooldown por-fingerprint: P0=900s, P1=7200s, P2=14400s
- [ ] ack suppression (`acked_until`) inclusive P0
- [ ] template + emoji P0🔴/P1🟡/P2🔵 + comandos rápidos
- [ ] curl→deilebot OU fallback log-only; emit `monitor.notify ... ok=... channel=...`

### Steer commands (`/state/monitor-commands/`)
- [ ] status / pause [dur] / resume / force-tick / ack <fp> / unknown
- [ ] auto-resume `from=auto` + emit `monitor.command`

### State / resiliência
- [ ] chaves: last_tick, last_tick_epoch, known_anomalies{first_seen,last_notified,count,acked_until}, notifications_this_hour, hour_slot, paused_until, fu_fingerprints, fu_created_today, fu_day_slot
- [ ] kube-api DNS-first (svc → svc.cluster.local → SERVICE_HOST) timeout 3s; falha → skip V1/V2/V6/V7
- [ ] graceful: deilebot down / gh rate-limit / RBAC denied → log + continua (sem crash)

### Manifest / config
- [ ] shell loop: Phase A sempre + Phase B condicional; intervalo 1800; DEILE_MAX_TOOL_ITERATIONS=50
- [ ] RBAC: secrets[get,patch,create] claude-credentials + deployments[get] claude-worker
- [ ] painel: tick_interval_s/preferred_model/notify-user-id lidos pelo `_panel_monitor.py` continuam válidos

### Segurança (achados da revisão cética)
- [ ] **Prompt injection** (HIGH): o `monitor-judgment.json` carrega `snippet` de comentários
      públicos do forge. NÃO é passado como argv do prompt da Phase B (`$(cat ...)` removido) —
      a mensagem é instrução FIXA do operador; a Phase B lê o arquivo via `read_file` e a persona
      tem guard explícito tratando snippets como DADO não-confiável (nunca instruções). Risco
      residual (LLM com bash/gh vê texto não-confiável) é o mesmo pré-refactor; FU: sandbox de tool.
- [ ] **Re-escalonamento de FP** (custo): a Phase B grava `fu_fingerprints` em TODA decisão terminal
      (create / not_a_promise / already_tracked), não só em create — senão um comentário FP
      re-escalaria a Phase B (LLM) a cada tick por 24h. Skip por cap é exceção (deve reescalar).
