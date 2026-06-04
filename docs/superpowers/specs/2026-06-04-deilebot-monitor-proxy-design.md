# Design — deilebot ↔ deile-monitor (canal de perguntas, status e ordens)

> Data: 2026-06-04 · Branch deile: `feat/deilebot-monitor-proxy` · Branch deilebot: `feat/monitor-proxy`
> Abordagem escolhida (pelo Humano): **Abordagem 1 — `deile-monitor` vira um alvo de dispatch on-demand**, escopo completo (Q&A + status determinístico + ordens + conserto do `/v1/notify`).

## 1. Problema

O `deilebot` (Discord) hoje **não consegue falar com o `deile-monitor`**:

- O `deile-monitor` é um loop de tick de uma execução só (`while true; monitor_tick.py; sleep 1800` em `infra/k8s/manifests/55-deile-monitor-deployment.yaml`). Entre ticks o processo Python não existe; todo estado vive no PVC RWO `deile-monitor-state` montado em `/state`. **Não tem servidor HTTP, não tem Service, não tem ingress** (a NetworkPolicy tem só uma regra de ingress comentada como "currently unused").
- O `deilebot` tem `automountServiceAccountToken: false` → **sem kubectl/RBAC**, sem token de forge, e a NetworkPolicy não o deixa alcançar `:8768`/`:8767`. Ele só fala HTTP com `deile-worker:8766` (via `worker-bearer`) e serve `:8765`.
- O `deile-worker` (alvo natural de dispatch) **também não tem kubectl** e está cego para pods/OAuth/pipeline. **Só o pod `deile-monitor` enxerga o cluster inteiro** (kubectl + forge + `/state`).

Além disso: a rota `POST /v1/notify` que o monitor usa para mandar DM (`infra/k8s/monitor_core.py:384`) **não existe** no control-plane do bot (`deilebot/.../runtime/control_plane/routes.py` não a registra) → hoje as notificações do monitor **caem em fallback log-only** (o Humano não recebe DM). É um bug latente que precisa ser consertado para qualquer canal bot↔monitor funcionar.

## 2. Objetivo

Permitir que o Humano, pelo Discord, possa:

1. **Perguntar ao monitor** sobre cluster/k8s/pipeline em linguagem natural — respondido por um DEILE rodando a persona de monitor (read-only) **dentro do pod `deile-monitor`**, o único que enxerga tudo.
2. **Consultar status determinístico** (sem LLM) — último tick, anomalias abertas, se está pausado.
3. **Enviar ordens** — `pause`/`resume`/`ack`/`force-tick`.
4. Receber **DM no Discord** das respostas e dos alertas proativos (conserto do `/v1/notify`).

Princípio condutor: **máximo reuso** dos padrões já existentes (contrato dispatch+persona, padrão de servidor `pipeline_status_server.py`/`worker_server.py`, padrão de bearer-secret compartilhado, a persona `monitor`, o invocador `wrapper.py monitor`, a fila `/state/monitor-commands/` já consumida pelo tick).

## 3. Arquitetura

### 3.1 `deile-monitor` vira o terceiro alvo de dispatch

Hoje há dois alvos de dispatch (`deile-worker`, `claude-worker`). O `deile-monitor` passa a ser o **terceiro — o "alvo ciente do cluster"**: o único com kubectl + forge + estado.

**Decisão de modelo de processo** (3 opções avaliadas):

| Opção | Descrição | Veredito |
|---|---|---|
| (a) processo em background no container do tick | servidor `&` antes do `while` | ❌ frágil, sem liveness próprio |
| (b) **servidor como processo principal**, que agenda o tick como subprocess | `monitor_command_server.py` substitui o `while` do bash e spawna `monitor_tick.py` por tick | ✅ **escolhida** |
| (c) container sidecar | 2º container no pod | ❌ duplica volumes; force-tick atravessa PID namespace |

**Escolhida: (b).** O `command` do container (manifest 55) muda de `["/bin/sh","-c", <while loop>]` para `["python3","/app/monitor_command_server.py"]`. O servidor:

- **Mantém o tick determinístico 100% byte-idêntico**: a cada `DEILE_MONITOR_TICK_INTERVAL_S` ele roda `python3 /app/monitor_tick.py` **como subprocess** (igual ao loop bash de hoje — mesma Phase A, zero LLM, isolamento de crash: bug no tick não derruba o servidor).
- **Replica a Phase B exatamente**: se `/state/monitor-judgment.json` existe após o tick, roda `python3 /app/wrapper.py monitor "<instrução fixa>"` como subprocess (a MESMA instrução fixa de hoje — defesa de prompt-injection preservada: o conteúdo do judgment nunca vira argv).
- **Serve aiohttp em `:8769`** concorrentemente (asyncio: `gather(tick_loop_task, web_server)`).
- **Espelha `deile/orchestration/pipeline/runner.py`** (que co-hospeda o loop do pipeline + o status server no mesmo event loop) — consistência arquitetural máxima.

**Defesa-em-profundidade (núcleo nunca quebra):** o startup do servidor web é `try/except` — se o bind falhar, **o loop de tick continua rodando**. A função-núcleo (vigiar o cluster) sobrevive a qualquer falha do HTTP. Liveness probe em `/v1/health` reinicia o pod se o servidor travar; cada tick é subprocess isolado.

### 3.2 Rotas do `monitor_command_server.py` (novo, `infra/k8s/`)

Bearer-gated (constant-time `hmac.compare_digest`), exceto `/v1/health`. Espelha o middleware de `pipeline_status_server.py:331-349` e o `_read_auth_token` (file → env).

| Método | Rota | Auth | Faz |
|---|---|---|---|
| GET | `/v1/health` | não | `{status: ok}` (liveness/readiness) |
| GET | `/v1/monitor-status` | sim | **Sem LLM.** Lê `/state/monitor-state.json` + flag `monitor-pause` + tail do `monitor-audit.log` → JSON `{last_tick, last_tick_epoch, age_s, paused, paused_until, known_anomalies:[{fp,severity,type,count,first_seen,last_seen}], notifications_this_hour, recent_events:[...]}` |
| POST | `/v1/command` | sim | Body `{command}`. Allowlist `pause <dur>`/`resume`/`ack <fp>`/`force-tick`. pause/resume/ack → escreve arquivo em `/state/monitor-commands/` (consumido pelo `_apply_steer` do próximo tick). force-tick → `touch /state/force-tick` (o scheduler interrompe o sleep). Retorna `{accepted, command, effect}` |
| POST | `/v1/ask` | sim | Body `{question, request_id?}`. Cria `request_id`, dispara em background `python3 /app/wrapper.py monitor_qa "<question>"` (persona read-only, ver §3.4), guarda o resultado. Retorna **202** `{request_id, status: "running"}` |
| GET | `/v1/ask/{request_id}` | sim | `{status: running|done|error, answer?, error?}`. **Espelha o padrão `POST /v1/dispatch` (nowait) + `GET /v1/result/{task_id}` do `deile-worker`** |

Force-tick unificado: o scheduler do tick aguarda o intervalo em fatias curtas, checando o flag `/state/force-tick` (poll ~5 s). O HTTP `force-tick` e o painel (`kubectl exec ... touch /state/force-tick`) usam o **mesmo** mecanismo de flag — substitui o `pkill -x sleep` de hoje (`_panel_monitor.py`), que deixa de existir (não há mais `sleep` para matar).

### 3.3 `MonitorClient` (novo, `deile/infrastructure/deile_monitor_client.py`)

Cópia endurecida do `deile_worker_client.py` (resolução de endpoint+token de arquivo-secret, timeouts httpx estruturados, erros tipados `MonitorClientError`). Vive no **repo deile** (o bot importa deile, como já faz com o worker client).

- Endpoint: `DEILE_MONITOR_ENDPOINT` (default `http://deile-monitor:8769`).
- Token: `DEILE_MONITOR_AUTH_TOKEN` → arquivo `/run/secrets/bot/monitor/MONITOR_BEARER_TOKEN` (mesma cadeia do worker).
- Métodos: `get_status()`, `post_command(cmd)`, `ask(question) -> request_id`, `get_ask_result(request_id)`.

### 3.4 Persona de Q&A read-only — `monitor_qa`

A persona `monitor` atual é uma persona de **tick** que toma ações de cura (deletar pod, renovar OAuth, abrir issue `[FU]`). Usá-la para Q&A livre seria perigoso (poderia mutar). Para "zero possibilidade de falha", o Q&A roda sob **`monitor_qa`** — a persona de monitor em **modo somente-leitura**:

- Instruções: "Você é o supervisor de cluster do DEILE em modo Q&A SOMENTE-LEITURA. Responda à pergunta do operador sobre cluster/pipeline/forge usando inspeção read-only (`kubectl get/describe/logs/top`, `gh`/`glab` de leitura). NUNCA mute: nada de `delete`/`patch`/`apply`/`edit`, `git push`, criar/editar/mergear issue ou PR, `rm`. Se a pergunta exigir mutação, explique o que faria — não faça."
- **Enforcement (não só instrução):** o subprocess de Q&A roda com o gate de risco do `bash_tool` configurado para **negar** comandos `dangerous` (sem auto-approve), e/ou um whitelist de tools read-only. A persona é o "como"; o gate é a garantia.
- Honra o pedido do Humano ("chamar um deile com a persona de monitor") preservando a linhagem da persona, mas com segurança por construção.

### 3.5 Lado do bot (repo deilebot)

1. **Conserta `/v1/notify`** (`runtime/control_plane/routes.py`): nova rota `POST /v1/notify`, body `{user_id, message, severity?}` (exatamente o que o monitor envia). Handler: DM via `adapter.send_dm` + persiste na conversation store (reusa o mecanismo do commit `fcdd33e`). Bearer automático (middleware existente). **Conserta o 404 latente.**
2. **Novo cog `/monitor`** (`cogs/monitor_cog.py`), **owner-gated** (reusa `admin_cog._is_owner`): subcomandos `status` (embed), `pause <dur>`/`resume`/`ack <fp>`/`force-tick` (→ `POST /v1/command`), `ask <pergunta>` (defer → `POST /v1/ask` → poll `GET /v1/ask/{id}` → edita a resposta). Componentes nativos do Discord (embeds) conforme preferência registrada.
3. **Auto-roteamento** (o pedido do Humano): quando uma mensagem de **owner** casa termos de cluster/k8s/pipeline, em vez de rodar o DEILE embedded (cego), o bot chama `MonitorClient.ask()` e responde. Conservador (owner-only, termos claros) para não sequestrar conversa normal.

### 3.6 Wiring (repo deile)

- **Service** `deile-monitor` (ClusterIP `:8769`, selector `app=deile-monitor`).
- **NetworkPolicy** (manifest 40): ingress `deile-monitor:8769` ← `deilebot`; egress `deilebot` → `deile-monitor:8769`. (Q&A usa kubectl no pod do monitor — não precisa de egress novo para `:8768`.)
- **Secret** `monitor-bearer` (key `MONITOR_BEARER_TOKEN`): auto-gerado por `deploy.py` (`_ensure_persisted_token`), montado em `deile-monitor` (servidor lê) e em `deilebot` (`/run/secrets/bot/monitor/`, wrapper expõe `DEILE_MONITOR_AUTH_TOKEN`).
- **`deploy.py`**: gera+aplica `monitor-bearer`; inclui os novos manifests no `k8s up`; adiciona `deile-monitor` aos `logs`/`status` (e à lifecycle, com cautela).
- **Dockerfile**: `COPY infra/k8s/monitor_command_server.py /app/` + `chmod 0555`; `.dockerignore`: `!infra/k8s/monitor_command_server.py`.
- **`wrapper.py`** (bot): `_wire_monitor_bearer()` espelhando `_wire_worker_bearer()`.
- **Manifest 55**: muda o `command` para o servidor; adiciona o volume `monitor-bearer`; adiciona a env `DEILE_MONITOR_*`; adiciona o Service. **`monitor_tick.py`/`monitor_core.py`/`monitor_vigias.py` ficam intocados.**
- **`_panel_monitor.py`**: force-tick passa a `touch /state/force-tick` (em vez de `pkill -x sleep`).
- **Persona**: `deile/personas/instructions/monitor_qa.md` + entrada em `deile/personas/library/` se necessário.

## 4. Fluxos de dados

**Pergunta:** Discord `/monitor ask "como tá o pipeline?"` → cog (defer) → `MonitorClient.ask()` → `POST :8769/v1/ask` → servidor spawna `wrapper.py monitor_qa` (kubectl/gh read-only no pod do monitor) → cog faz poll `GET /v1/ask/{id}` → edita a resposta no Discord. Em paralelo, o servidor pode mandar DM via `POST {bot}:8765/v1/notify`.

**Ordem:** `/monitor pause 30m` → cog (owner) → `POST :8769/v1/command {command:"pause 30m"}` → servidor escreve `/state/monitor-commands/<ts>` → próximo tick `_apply_steer` aplica (pausa). `force-tick` → `touch /state/force-tick` → scheduler roda o tick já.

**Status:** `/monitor status` → `GET :8769/v1/monitor-status` → lê `/state/*` → embed. Zero LLM.

**Alerta proativo:** tick detecta anomalia → `Notifier._deliver` → `POST {bot}:8765/v1/notify` (agora **existe**) → DM.

## 5. Tratamento de erro

- Servidor: bind do HTTP em `try/except` — o loop de tick nunca para por falha do HTTP. Cada tick/Phase B/Q&A é subprocess com timeout; erro vira log + estado `error`, nunca derruba o supervisor.
- `MonitorClient`: erros tipados (`MONITOR_AUTH_MISSING`, `MONITOR_TIMEOUT`, `MONITOR_UNREACHABLE`, `MONITOR_BAD_RESPONSE`), timeouts httpx estruturados (connect/pool capados), como no worker client.
- Cog: monitor indisponível → mensagem clara ("monitor não respondeu"), nunca trava a interação (defer + timeout de poll).

## 6. Segurança

- Transporte: Bearer constant-time em todas as rotas (exceto `/v1/health`); secret montado como arquivo (`/run/secrets/...`), nunca env.
- Autorização: o cog `/monitor` é **owner-only** (gate do bot). Auto-route só para owner.
- Q&A read-only por construção (§3.4): persona + gate de bash negando `dangerous`. O pod do monitor já tem esses poderes hoje (tick); o incremento é o Q&A, contido a leitura.
- NetworkPolicy: ingress ao `:8769` só do `deilebot`.
- Prompt-injection: Phase B mantém a instrução fixa (judgment nunca vira argv); Q&A é owner-gated e read-only.

## 7. Testes

**deile:** unit do `monitor_command_server.py` (status, command enqueue, ask lifecycle, bearer auth, allowlist, force-tick flag, servidor-falha-mas-tick-segue); unit do `deile_monitor_client.py` (resolução endpoint/token, mapeamento de erro); estende `test_monitor_packaging.py` (Dockerfile/.dockerignore do novo arquivo); garante `test_monitor_emit_schema.py` intacto; o supervisor não pode quebrar os testes de `monitor_tick.py` (intocados).

**deilebot:** handler `/v1/notify`; cog `/monitor` (mock `MonitorClient`); detecção de auto-route.

**Gate:** suíte completa verde nos dois repos antes de cada commit/PR. Validação live (deploy) fica **pendente por decisão do Humano** (não reiniciar o k8s) — declarado explicitamente no PR.

## 8. Plano de PRs (cross-repo)

- **PR deile** (`feat/deilebot-monitor-proxy`): servidor + client + persona + manifests + deploy.py + Dockerfile + wrapper + painel + testes. **Mergeia primeiro** (o bot importa o `MonitorClient` daqui).
- **PR deilebot** (`feat/monitor-proxy`): conserto `/v1/notify` + cog `/monitor` + auto-route + testes. Mergeia depois.

## 9. Riscos e mitigação

| Risco | Mitigação |
|---|---|
| Servidor como main process derruba o tick se travar | tick é subprocess isolado; HTTP em try/except; liveness reinicia; espelha runner.py (precedente em produção) |
| Sem validação live (k8s não reiniciado) | cobertura unit alta + manifest dry-run + revisão cética 5x; PR declara pendência de deploy |
| Q&A muta o cluster | persona read-only + gate de bash deny-dangerous (enforcement, não só instrução) |
| Painel force-tick quebra (`pkill sleep` some) | painel migra para `touch /state/force-tick` no mesmo PR |
| `origin/main` se move (pipeline mergeia) | rebase antes do merge |

## 10. Fora de escopo (follow-ups)

- Toolificar `:8768`/`:8767` para o agente embedded (Abordagem 3).
- Substituir o `kubectl exec` do painel por HTTP no `:8769`.
- Cockpit multi-alvo (pipeline + claude-worker) no bot.
