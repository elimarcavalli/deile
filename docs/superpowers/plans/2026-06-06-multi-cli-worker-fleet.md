# Frota multi-CLI: workers plugáveis (OpenCode, Codex, Qwen, Aider, Goose, Antigravity) + Claude como um entre vários — Plano de Implementação

> **Para workers agênticos:** SUB-SKILL recomendada — `superpowers:subagent-driven-development` ou `executing-plans`. Passos usam checkbox (`- [ ]`).

**Goal:** Generalizar o padrão `claude-worker` num framework de **N workers CLI plugáveis**, cada um rodando um agente de coding headless (one-shot) num pod K8s isolado, despachável por-stage pelo pipeline. O `claude-worker` passa a ser **um entre vários**; o operador escolhe, por stage, qual CLI + qual modelo, com o grosso roteável a providers baratos (DeepSeek/Qwen/Gemini via OpenRouter), neutralizando o lock de preço.

**Architecture:** Um **servidor genérico** (`cli_worker_server.py`) parametrizado por um **adapter por CLI** (selecionado por env `DEILE_CLI_WORKER_KIND`). O servidor reaproveita TODA a maquinaria genérica do `claude_worker_server.py` (lease/heartbeat, session-metadata, cleanup, workspace isolado, contrato HTTP `/v1/dispatch`); o adapter especializa apenas **5 pontos**: montar argv headless, parsear saída, listar modelos, env de auth, dirs graváveis. Integra ao `dispatch_resolver` (cada CLI vira um dispatcher válido) e ao painel (`DispatchMatrixView`). Imagens **per-tool** via build-arg (runtimes divergem). Plano separado integra **OpenRouter** ao DEILE CLI (deile-worker in-process) e como provider unificador da frota.

**Tech Stack:** Python 3.11 (aiohttp server), K8s (Rancher Desktop/k3s), Docker multi-target por build-arg, CLIs: OpenCode (binário), OpenAI Codex (rust/npm), Qwen Code (node22/npm), Aider (pip), Goose (binário), Antigravity `agy` (go binário, **gated**).

---

## PARTE 0 — Como a troca de modelo/worker funciona HOJE (estudo do código)

> Fonte: mapeamento do código em `infra/k8s/` e `deile/orchestration/pipeline/`. Trate file:line como aproximados (verificar no impl).

### 0.1 Eixos de configuração por-stage (já existem)
Três resolvers independentes, todos por-stage (`classify, refine, implement, pr_review, follow_ups`):

| Eixo | Resolver | Env per-stage | Env global | Settings.json | Default |
|---|---|---|---|---|---|
| **Worker** | `dispatch_resolver.resolve_stage_dispatcher()` | `DEILE_PIPELINE_DISPATCH_<STAGE>` | `DEILE_PIPELINE_DISPATCH_MODE` | `pipeline.dispatch.<stage>` | `deile-worker` |
| **Modelo** | `model_resolver.resolve_stage_model()` | `DEILE_PIPELINE_MODEL_<STAGE>` | `DEILE_PREFERRED_MODEL` | `pipeline.models.<stage>` | `None`→worker decide |
| **Reasoning** | `reasoning_resolver.resolve_stage_reasoning()` | `DEILE_PIPELINE_REASONING_<STAGE>` | `DEILE_REASONING_EFFORT` | `pipeline.reasoning.<stage>` | opinado por stage |

- `VALID_DISPATCHERS = {"deile-worker", "claude-worker"}` (`dispatch_resolver.py`). **Ponto de extensão #1.**
- Endpoints: `deile-worker→http://deile-worker:8766`, `claude-worker→http://claude-worker:8767` (override por `DEILE_*_WORKER_ENDPOINT`). **Ponto de extensão #2.**
- Os três valores viajam no **DispatchPayload** (`deile/infrastructure/deile_worker_client.py`): `preferred_model`, `preferred_reasoning`, `stage`, `branch`, `resume_session_id`, `prev_task_id`, `timeout_s`, `max_retries`, `brief`, `channel_id`. **Ponto de extensão #3** (relaxar validação `provider:model` para CLIs).

### 0.2 Quem executa o dispatch
`deile/orchestration/pipeline/implementer.py` — `WorkerImplementer` resolve `dispatcher = resolve_stage_dispatcher(stage)`, pega `endpoint = get_endpoint_for(dispatcher)`, faz `POST /v1/dispatch` (fire-and-forget `wait_for_result=False` + reconcile via `/v1/progress` no próximo tick, com `DispatchLedger`). **Genérico — funciona para qualquer worker que respeite o contrato HTTP.**

### 0.3 Maquinaria genérica do claude-worker (REAPROVEITAR)
`infra/k8s/claude_worker_server.py`: lease/heartbeat (`.lease.json`, TTL 30s, hb 5s), session-metadata (`~/.../tasks/<task_id>/session.json` para resume), workspace isolado (`$ROOT/<task_id>`), `startup_cleanup()` (GC de workdirs stale), contrato HTTP (`/v1/dispatch`, `/v1/health`, `/v1/progress`, `/v1/pod-status`). **Tudo isto é agnóstico de CLI** e vira o core compartilhado.

### 0.4 Específico-do-claude (NÃO reaproveitar — vira adapter)
argv mounting (`claude -p --permission-mode bypassPermissions --output-format json --model --effort`), parsing do `--output-format json`, OAuth handling (todo o `_claude_creds_refresh`, initContainer bootstrap-creds, CronJob renew), `_coerce_claude_effort`, `_ULTRACODE_PREAMBLE`.

### 0.5 Painel
`DispatchMatrixView` (tecla `[d]`): matriz stages × (Worker, Model, Reasoning); seta via `kubectl set env deploy/deile-pipeline DEILE_PIPELINE_*`. **Ponto de extensão #4** (incluir novos workers + buscar modelos via `/v1/models`).

### 0.6 deploy.py + manifests + Dockerfile
`K8S_DEPLOYMENTS` tuple lista os deployments; manifests numerados (`47–52` claude-worker); NetworkPolicy (`40`) com ingress-from-pipeline + egress-LLM-e-forges; Dockerfile single-image `deile-stack:local` com build-arg `WITH_BOT`. **Padrões a clonar por worker.**

---

## PARTE 1 — Arquitetura-alvo: framework de CLI workers

### 1.1 Servidor genérico `infra/k8s/cli_worker_server.py`
Extrai do `claude_worker_server.py` o **core agnóstico** (lease, heartbeat, session-meta, cleanup, HTTP, workspace) para um módulo compartilhado `infra/k8s/_worker_core.py`, e implementa o servidor genérico que delega ao **adapter** selecionado por `DEILE_CLI_WORKER_KIND`.

> **Decisão:** NÃO reescrever o `claude_worker_server.py` agora (risco). Em vez disso: (a) **extrair** o core para `_worker_core.py`, (b) o `claude_worker_server.py` passa a importar o core (refactor de baixo risco, testado), (c) o novo `cli_worker_server.py` usa o mesmo core. Claude vira "só mais um adapter" conceitualmente, mas mantém seu server dedicado por causa do OAuth (que os outros não têm). **Resultado:** core único, dois servers (claude-com-OAuth e cli-genérico), N adapters no cli-genérico.

### 1.2 Interface do adapter — `infra/k8s/cli_adapters/base.py`
```python
class CliAdapter(Protocol):
    kind: str                      # "opencode" | "codex" | ...
    default_port: int

    def build_argv(self, *, brief_path: str, model: str | None,
                   reasoning: str | None, workdir: str,
                   resume: ResumeCtx | None) -> list[str]: ...
    def env_overlay(self, *, home: str) -> dict[str, str]: ...   # HOME/XDG/CONFIG + config inline
    def parse_output(self, *, stdout: str, stderr: str, rc: int) -> WorkResult: ...
    def list_models(self) -> list[ModelInfo]: ...                # alimenta GET /v1/models
    def auth_env_keys(self) -> list[str]: ...                    # p/ validação de readiness
    def writable_dirs(self) -> list[str]: ...                    # p/ readOnlyRootFilesystem
    git_strategy: Literal["cli_autocommit", "wrapper_commits", "brief_driven"]
```
- `WorkResult`: `ok, result_text, error_code, cost_usd|None`.
- `ResumeCtx`: `session_id, prev_task_id` (para CLIs que suportam resume; senão ignora → fresh).
- **Registro**: `ADAPTERS = {a.kind: a for a in (OpenCodeAdapter(), CodexAdapter(), ...)}`; server escolhe por `DEILE_CLI_WORKER_KIND`.

### 1.3 Modelo: como viaja e como o worker mostra os suportados
- **Viagem:** `DispatchPayload.preferred_model` deixa de exigir `provider:model` quando o destino é CLI worker. Para CLIs, o valor é o **model-id nativo do CLI** (ex.: `openrouter/anthropic/claude-3.7-sonnet`, `qwen3-coder-plus`, `gpt-5.5-codex`). **Mudança:** relaxar o validator do `preferred_model` (aceitar string livre quando `stage`/dispatcher é CLI) OU adicionar campo `cli_model: str|None` separado. **Escolha:** adicionar `cli_model` separado (não quebra o validator existente do deile-worker). O `model_resolver` ganha `resolve_stage_cli_model(stage)` lendo `DEILE_PIPELINE_MODEL_<STAGE>` como string livre.
- **"Cada worker mostra os modelos que suporta":** cada worker expõe **`GET /v1/models`** → `[{id, label, provider, context?, notes?}]`. Duas estratégias por adapter:
  - **Dinâmica** (preferida quando há comando): roda o list do CLI e parseia. `opencode models`, `aider --list-models ""`. Cacheado (TTL) porque pode tocar a rede (models.dev).
  - **Estática/catálogo**: lista curada no adapter (Codex, Qwen, Goose, Antigravity — não têm `list-models` confiável). Documentar fonte.
- O painel chama `GET /v1/models` do worker selecionado e popula o picker de modelo daquele stage. **Isto satisfaz o requisito explícito.**

### 1.4 Autonomia (sem TTY → qualquer prompt trava) — flags por CLI
| Worker | Flag/Env de autonomia total |
|---|---|
| opencode | `--dangerously-skip-permissions` **+** config `{"permission":"allow"}` via `OPENCODE_CONFIG_CONTENT` |
| codex | `--dangerously-bypass-approvals-and-sandbox` (alias `--yolo`); ou `--sandbox workspace-write` + `[sandbox_workspace_write] network_access=true` |
| qwen | `--yolo` (= `--approval-mode yolo`) + `QWEN_CODE_UNATTENDED_RETRY=1` |
| aider | `--yes-always` |
| goose | env `GOOSE_MODE=auto` |
| antigravity | `--dangerously-skip-permissions` (⚠️ não-confirmado oficialmente) |

### 1.5 Git/commit — estratégia por adapter (`git_strategy`)
- **aider** = `cli_autocommit`: roda com `--auto-commits`; o **wrapper faz `git push` + abre PR** (aider não faz push/PR). Mensagens Conventional Commits pelo weak-model. Usar `--no-attribute-author` conforme regra do projeto (sem "(aider)"/Co-Authored-By).
- **opencode/codex/qwen/goose/antigravity** = `brief_driven`: o brief instrui o agente a `git add/commit/push` via tool bash/shell (todos têm, sob auto-approve). O **wrapper valida** que houve commit+push (checa `git log`/branch remota) e, se não, faz fallback commit. **Identidade git** (`user.name`/`user.email`) + token (GITHUB_TOKEN/GITLAB_TOKEN) injetados no env.
- O **brief unificado** (`briefs.py`) ganha uma variante neutra-de-CLI (sem jargão claude) reusando o `cli_renderer` forge-agnóstico que já existe.

### 1.6 Exit code NÃO é confiável (vale p/ TODOS exceto parsing próprio)
Pesquisa confirmou: opencode/codex/qwen/goose/aider **não garantem exit-code 0/≠0 limpo** para gate. **Regra do core:** o `WorkResult.ok` é decidido pelo adapter via parse de saída **E** por um **gate pós-execução do wrapper** (git diff aplicado? push feito? `pytest` se o brief pediu?). Nunca confiar só em `$?`. (Espelha o que o pipeline já faz com o quality-gate.)

### 1.7 readOnlyRootFilesystem — dirs graváveis por CLI (todos non-root uid 10001)
| Worker | Dirs graváveis / env | Observações |
|---|---|---|
| opencode | `HOME`, `XDG_DATA_HOME=/data`; config via `OPENCODE_CONFIG_CONTENT` (inline, evita arquivo) | binário standalone; `opencode upgrade` proibido (pin) |
| codex | `CODEX_HOME=/data/codex`; workdir gravável | rust musl binary |
| qwen | `HOME` + `~/.qwen`; workdir | **node>=22** (conflito com node20 do claude → imagem própria) |
| aider | `HOME` (config/cache litellm) + workdir (history files); `--no-gitignore` | pip; precisa `git` |
| goose | `HOME`/`XDG_CONFIG_HOME` + `~/.config/goose`; **`GOOSE_DISABLE_KEYRING=1`** | binário; keyring/DBus quebra sem isso |
| antigravity | `HOME` + `~/.gemini/...`; keyring p/ OAuth (⚠️) | go binary |
Padrão K8s: montar `emptyDir`/PVC em `/home/<kind>` e `/data`; `HOME` aponta pra lá; resto read-only.

### 1.8 Estratégia de imagem — **per-tool via build-arg**
Runtimes divergem (node20 vs node22 vs pip vs binário) → **não cabe imagem única**. Criar `infra/k8s/Dockerfile.cli-worker` com `ARG WORKER_KIND` e blocos de install condicionais (mirror do `WITH_BOT`). Imagens: `deile-cli-worker-<kind>:local`. Camada-base comum (python server + git/gh/glab/kubectl + `_worker_core.py` + adapters) compartilhada; layer final instala só o CLI do kind. `deploy.py` ganha `k8s build-cli-workers [--kind <k>]`.

> **Tradeoff documentado:** 6 imagens novas (~500MB–1GB cada) em disco local. Aceitável no Rancher Desktop. Alternativa rejeitada: imagem-fat única (quebra por conflito node20/22 + bloat). Mitigação: base layer compartilhada via cache.

### 1.9 Dispatcher + endpoints + manifests por worker
- `dispatch_resolver.VALID_DISPATCHERS` += `{opencode-worker, codex-worker, qwen-worker, aider-worker, goose-worker, antigravity-worker}`.
- Endpoints: `<kind>-worker:<porta>` (porta única por worker, ex. 8771–8776). Override por `DEILE_<KIND>_WORKER_ENDPOINT`.
- Por worker, clonar o conjunto de manifests do claude-worker, **menos OAuth**: Deployment (cmd `python3 /app/cli_worker_server.py` + `DEILE_CLI_WORKER_KIND=<kind>`), Service, PVC (ou emptyDir se sem resume), allowed-repos ConfigMap (reusar o mesmo), cleanup CronJob (reusar core), bearer Secret, NetworkPolicy (ingress-from-pipeline + egress 443 LLM/forge — ajustar hosts por CLI: openrouter.ai, dashscope, api.openai.com, etc. + DNS).
- Secrets de auth: **um Secret compartilhado `cli-worker-keys`** com `OPENROUTER_API_KEY` (+ opcionalmente chaves diretas). Cada Deployment monta as env vars que seu adapter declara em `auth_env_keys()`.

### 1.10 Painel + deploy.py
- `DispatchMatrixView`: dropdown de Worker inclui os novos; ao escolher um worker num stage, busca `GET /v1/models` daquele worker p/ popular o picker de modelo; reasoning column só p/ workers que suportam (claude/codex/qwen têm; aider/goose não → desabilita).
- `deploy.py`: `K8S_DEPLOYMENTS` += novos; `k8s build-cli-workers`, `k8s up` aplica manifests novos; `k8s scale --<kind>-worker N`.

---

## PARTE 2 — Specs por worker (o "como" literal)

> Auth recomendada para TODOS (exceto claude/antigravity): **API key via env (não expira)** — evita o inferno de refresh-token. Preferir **OpenRouter** (uma chave) onde possível.

### 2.1 opencode-worker  ⭐ (Tier 1 — primeiro a implementar)
- **Headless:** `opencode run --dir <workdir> -m <model> --dangerously-skip-permissions --format json -f <brief_path> "Implemente conforme o brief anexado."`
- **Modelo:** `-m provider/model` (ex. `openrouter/anthropic/claude-3.7-sonnet`). Config inline: `OPENCODE_CONFIG_CONTENT='{"$schema":"https://opencode.ai/config.json","permission":"allow"}'`.
- **list_models:** `opencode models` (dinâmico; cache TTL; `--refresh` toca models.dev → whitelist models.dev no egress OU catálogo estático fallback).
- **Auth:** `OPENROUTER_API_KEY` (ou `ANTHROPIC_API_KEY`/`OPENAI_API_KEY`/`DEEPSEEK_API_KEY`). `{env:VAR}` na config.
- **Dirs:** `HOME`, `XDG_DATA_HOME`. **git:** brief_driven. **Pin** versão; nunca `upgrade`.
- **Gotcha:** confirmar `--dangerously-skip-permissions` existe na versão pinada (`opencode run --help`); fallback = só `permission:"allow"`. stdin não-oficial → usar `-f`.

### 2.2 codex-worker (Tier 2)
- **Headless:** `codex exec --sandbox workspace-write --json -o /work/out.json - < brief` (stdin) **ou** `codex exec --dangerously-bypass-approvals-and-sandbox --json "<brief>"`.
- **Modelo:** `-m gpt-5.5-codex` (ou via config.toml `model=`). Provider custom/OpenRouter: bloco `[model_providers.<id>]` com `base_url`+`env_key`, **mas `wire_api="responses"` é obrigatório** → **só providers que falam Responses API** (OpenRouter: validar por modelo; muitos só Chat Completions → **podem não funcionar**). ⚠️ Documentar: codex-worker é melhor com OpenAI direto.
- **list_models:** sem comando → **catálogo estático** no adapter (gpt-5.5, gpt-5.5-codex, …).
- **Auth:** `OPENAI_API_KEY` (não expira). **Dirs:** `CODEX_HOME`. **git:** brief_driven (não auto-commita).
- **Gotcha:** sempre `codex exec` (nunca `codex` puro — pode panicar sem TTY). Exit-code grosso → parse JSONL.

### 2.3 qwen-worker (Tier 2 — melhor custo)
- **Headless:** `qwen -p "<brief>" --yolo --output-format json` (+ `QWEN_CODE_UNATTENDED_RETRY=1`, capar com timeout do pod).
- **Modelo:** `OPENAI_MODEL=qwen3-coder-plus` + tríade `OPENAI_API_KEY`+`OPENAI_BASE_URL`. Via **OpenRouter**: `OPENAI_BASE_URL=https://openrouter.ai/api/v1`, `OPENAI_MODEL=qwen/qwen3-coder`. Multi-provider de fato via base_url.
- **list_models:** sem comando → catálogo estático (qwen3-coder-plus/next/480b + o que o base_url expõe).
- **Auth:** `OPENAI_API_KEY` (OAuth free tier **morto** desde 2026-04-15). **Dirs:** `HOME`+`~/.qwen`. **node>=22** → imagem própria. **git:** brief_driven.

### 2.4 aider-worker (Tier 1 — cirúrgico)
- **Headless:** `aider --model <prov/model> --message-file <brief> --yes-always --no-stream --no-pretty --analytics-disable --no-check-update --no-gitignore --auto-commits` (+ `--auto-test --test-cmd "pytest -q"` p/ loop de verificação).
- **Modelo:** `--model openrouter/anthropic/claude-3.7-sonnet` | `deepseek/deepseek-chat` | `gemini/...`. `--weak-model` p/ commit msgs barato.
- **list_models:** `aider --list-models ""` (dinâmico).
- **Auth:** `OPENROUTER_API_KEY`/`DEEPSEEK_API_KEY`/etc. **Dirs:** `HOME`+workdir. precisa `git`. **git:** `cli_autocommit` (auto-commit; wrapper faz push+PR; `--no-attribute-*` p/ regra do projeto).
- **Gotcha:** `--message --yes-always` é single-pass → **gate pós-run obrigatório** (build/test) porque pode commitar código quebrado.

### 2.5 goose-worker (Tier 1)
- **Headless:** `GOOSE_MODE=auto goose run --no-session -q --output-format json -t "<brief>"` (ou `-i -` stdin). `--max-turns <teto>` baixo p/ custo.
- **Modelo:** `GOOSE_PROVIDER` + `GOOSE_MODEL` (ou `--provider/--model` por invocação). OpenRouter: `GOOSE_PROVIDER=openrouter`+`OPENROUTER_API_KEY`+`GOOSE_MODEL=anthropic/claude-sonnet-4`. OpenAI-compat custom: `GOOSE_PROVIDER=openai`+`OPENAI_HOST`.
- **list_models:** sem comando → catálogo estático.
- **Auth:** chave do provider. **`GOOSE_DISABLE_KEYRING=1` obrigatório** (DBus quebra). **Dirs:** `~/.config/goose` gravável + `HOME`. **git:** brief_driven (Developer extension dá shell+text_editor).
- **Gotcha:** instalar com `CONFIGURE=false`; exit-code não-confiável; `GOOSE_MODE=auto` reportado falho com provider `claude-code` (#3386) — usar com OpenRouter/OpenAI.

### 2.6 antigravity-worker (Tier 3 — ⚠️ GATED por spike)
- **Status honesto:** closed-source, **auth headless por API key não suportada no consumer** (issue #78 aberta), Google-locked (modelos não-Google só via harness Google), `--print` sem conversation-ID por chamada (#7). Doc oficial JS-only (flags não-confirmadas literalmente).
- **GATE obrigatório (Task dedicada, ANTES de qualquer manifest):** spike num container — `agy --help`, `agy auth --help`, testar auth headless via **Vertex/Gemini Enterprise service-account** (`GOOGLE_APPLICATION_CREDENTIALS` + `GOOGLE_CLOUD_PROJECT`), e `agy -p "echo" --output-format json`. **Só prosseguir se o spike provar auth headless + one-shot determinístico.** Senão: **não implementar** — usar Gemini via OpenRouter/OpenCode ou via deile-worker (provider google) — registrar como "bloqueado, reavaliar quando #78 fechar".
- **Headless (SE spike passar):** `agy -p "<brief>" --dangerously-skip-permissions --output-format json --print-timeout <t>`. **Modelo:** `-m gemini-3.1-pro` (catálogo estático). **Dirs:** `HOME`+`~/.gemini` + (service-account JSON via Secret). **git:** brief_driven.

### 2.7 claude-worker (já existe — agora "um entre vários")
- Mantém server dedicado (`claude_worker_server.py`) por causa do OAuth. Passa a importar `_worker_core.py`. No painel, aparece como mais um dispatcher. Usado pelo operador **pra tarefas específicas** (review crítico, arquitetura) — não default. Auth permanece o caminho oficial (setup-token quando migrar; ver plano OAuth separado já discutido).

---

## PARTE 3 — Plano SEPARADO: DEILE CLI + OpenRouter

> Independente da frota CLI; habilita o **deile-worker in-process** e o **DEILE CLI local** a falar OpenRouter (uma chave → todos os providers), e serve de provider unificador para os CLI workers.

### 3.1 Registrar OpenRouter no DEILE
- `deile/config/model_providers.yaml`: adicionar provider `openrouter` (OpenAI-compatible; `base_url=https://openrouter.ai/api/v1`; `api_key_env=OPENROUTER_API_KEY`; lista de modelos curada DeepSeek/Qwen/Gemini/Claude/GPT com tiers/custo).
- `deile/core/models/bootstrap.py` (`bootstrap_providers`): registrar o provider OpenRouter (reusar o adapter OpenAI-compatible existente; OpenRouter é OpenAI-compatible). Confirmar via context7/SDK que o provider OpenAI aceita `base_url` custom.
- Headers OpenRouter opcionais (`HTTP-Referer`, `X-Title`) — best-effort.

### 3.2 Secret + propagação
- `.env`: `OPENROUTER_API_KEY` (gitignored). `deploy.py k8s up`: propaga p/ Secret `deile-secrets` + monta no deile-worker e (se usado) no DEILE CLI. **É segredo do operador → só Secret, nunca no repo.**

### 3.3 Roteamento por-stage barato
- Per-stage models passam a aceitar `openrouter:deepseek/deepseek-chat` etc. Documentar tabela de custo (DeepSeek $0,78/M out vs Opus) no painel/doc p/ decisão consciente.

---

## PARTE 4 — Task breakdown (TDD, bite-sized, fases)

### Fase A — Core compartilhado (fundação; sem isto nada funciona)
- [ ] **A1** Criar `infra/k8s/_worker_core.py` extraindo do `claude_worker_server.py`: lease/heartbeat, session-meta, workspace, `startup_cleanup`, helpers HTTP (auth bearer, health). **Teste:** mover/duplicar os testes existentes do claude-worker que cobrem essas funções, apontando p/ o core. Suíte verde.
- [ ] **A2** Refatorar `claude_worker_server.py` p/ importar do `_worker_core.py` (sem mudança de comportamento). **Teste:** suíte do claude-worker continua verde; smoke `/v1/health`.
- [ ] **A3** Definir `cli_adapters/base.py` (`CliAdapter` Protocol, `WorkResult`, `ResumeCtx`, `ModelInfo`) + registro. **Teste:** unit do registro + um FakeAdapter.
- [ ] **A4** `cli_worker_server.py`: server genérico que carrega adapter por `DEILE_CLI_WORKER_KIND`, expõe `/v1/dispatch`, `/v1/health`, `/v1/progress`, **`/v1/models`**. Gate pós-run (commit/push/test) no core. **Teste:** dispatch contra FakeAdapter (sem rede); `/v1/models` retorna lista do adapter; gate detecta "sem commit".

### Fase B — Integração de roteamento (pipeline enxerga os novos workers)
- [ ] **B1** `dispatch_resolver.py`: estender `VALID_DISPATCHERS` + aliases + endpoints (`<kind>-worker:<porta>`, env `DEILE_<KIND>_WORKER_ENDPOINT`). **Teste:** `resolve_stage_dispatcher` aceita cada novo kind; endpoint resolve.
- [ ] **B2** `model_resolver.py`: `resolve_stage_cli_model(stage)` (string livre). `DispatchPayload`: campo `cli_model: str|None` (não quebra validator `provider:model`). **Teste:** payload aceita cli_model; deile-worker ignora; cli-worker usa.
- [ ] **B3** `implementer.py`/`stages.py`: ao dispatchar p/ um `*-worker` CLI, enviar `cli_model`/reasoning/brief no payload p/ o endpoint certo. **Teste:** mock HTTP confirma POST no endpoint do worker certo com cli_model.

### Fase C — Imagem + um worker piloto (opencode) ponta-a-ponta
- [ ] **C1** `infra/k8s/Dockerfile.cli-worker` com `ARG WORKER_KIND` (base comum + install condicional). Implementar bloco `opencode` (binário pinado). **Teste:** build `--build-arg WORKER_KIND=opencode` verde; `opencode --version` no container.
- [ ] **C2** `cli_adapters/opencode.py` (build_argv, env_overlay, parse_output, list_models via `opencode models`, auth_env_keys, writable_dirs, git_strategy=brief_driven). **Teste:** unit de build_argv (flags exatas) + parse de saída JSON real capturada.
- [ ] **C3** Manifests `opencode-worker` (Deployment+Service+PVC+bearer+NetworkPolicy; reusar allowed-repos ConfigMap). `deploy.py` K8S_DEPLOYMENTS += ; `k8s build-cli-workers --kind opencode`. **Teste:** `k8s up` sobe pod Ready; `/v1/health` 200; `/v1/models` lista.
- [ ] **C4** **E2E piloto:** apontar `DEILE_PIPELINE_DISPATCH_IMPLEMENT=opencode-worker` + `DEILE_PIPELINE_MODEL_IMPLEMENT=openrouter/deepseek/deepseek-chat`, soltar uma issue simples, observar PR aberta pelo opencode-worker. **Prova real.** Reverter dispatch ao normal após.

### Fase D — Demais adapters (replicar o padrão C2+C3, um por vez, cada um com E2E)
- [ ] **D1** aider-worker (git_strategy=cli_autocommit; gate build/test; `--no-attribute-*`). E2E.
- [ ] **D2** goose-worker (`GOOSE_DISABLE_KEYRING=1`, `CONFIGURE=false`, `GOOSE_MODE=auto`). E2E.
- [ ] **D3** qwen-worker (imagem node22; tríade OPENAI_*; OpenRouter). E2E.
- [ ] **D4** codex-worker (OPENAI_API_KEY; `wire_api=responses` caveat documentado; preferir OpenAI direto). E2E.

### Fase E — Antigravity (GATED)
- [ ] **E1** **SPIKE** (não-código): instalar `agy` num container, validar auth headless (Vertex service-account) + `agy -p ... --output-format json`. **Decisão de prosseguir/abortar registrada no plano.**
- [ ] **E2** (só se E1 passar) adapter + manifests antigravity-worker (Vertex SA Secret). E2E. Senão: documentar bloqueio + usar Gemini via OpenRouter.

### Fase F — Painel + OpenRouter no DEILE (Parte 3) + docs
- [ ] **F1** `DispatchMatrixView`: incluir novos workers; picker de modelo via `GET /v1/models`; reasoning só onde suportado. **Teste:** test do painel (largura adaptativa + workers listados).
- [ ] **F2** Parte 3: OpenRouter provider no `model_providers.yaml` + `bootstrap_providers` + Secret propagation. **Teste:** bootstrap registra openrouter; smoke 1-msg via OpenRouter (custo mínimo).
- [ ] **F3** Docs: `docs/system_design/` nova seção "Frota multi-CLI" + atualizar CLAUDE.md (mapa de workers, env vars, portas). Decisão em `DECISOES.md`.

### Fase G — Validação final
- [ ] **G1** Suíte completa verde (`pytest deile/tests/ -q` com coverage gate). Multi-seed ordering (0/1/2/42/last).
- [ ] **G2** `k8s up` do zero sobe a frota inteira; cada worker `/v1/health` + `/v1/models`; matriz no painel troca worker×model×reasoning por stage e o pipeline respeita.
- [ ] **G3** Commit por fase (revisão + 100% verde antes de cada commit — protocolo multi-provider do operador). PRs pequenas por fase.

---

## PARTE 5 — Revisão cética (riscos, inconsistências, correções)

> Auto-crítica "onde isso quebra?" — feita após escrever o plano.

1. **Conflito de runtime Node (qwen22 vs claude20)** → RESOLVIDO por imagens per-tool (1.8). Não tentar imagem única.
2. **Exit code não-confiável em TODOS** → RESOLVIDO por gate pós-run no core (1.6); nunca confiar em `$?`. Risco residual: detectar "sucesso real" exige heurística (commit+push presente / testes verdes) — encapsular no core e cobrir por teste.
3. **Modelo `provider:model` vs CLI-native** → RESOLVIDO por campo `cli_model` separado (não quebra validator do deile-worker). Risco: painel precisa saber se um stage usa CLI worker (string livre) ou deile-worker (provider:model) → o picker decide pelo worker selecionado.
4. **Codex `wire_api=responses`** limita OpenRouter/OpenAI-compat → codex-worker é confiável **só com OpenAI direto**; documentado; não vender codex+OpenRouter sem validar por modelo. (Inconsistência com "tudo via OpenRouter" — corrigida: codex é exceção.)
5. **Antigravity auth headless** pode não existir no consumer → GATED por spike (Fase E); plano NÃO assume que funciona. Honesto.
6. **Goose `GOOSE_MODE=auto` + provider claude-code** falho (#3386) → goose-worker usa OpenRouter/OpenAI, não claude-code provider.
7. **opencode `--dangerously-skip-permissions`** divergência doc×issue → validar na versão pinada; fallback `permission:"allow"`. Encodado como gotcha + teste de smoke `--help`.
8. **readOnlyRootFilesystem** quebra cada CLI de um jeito (keyring goose, XDG opencode, CODEX_HOME, ~/.qwen) → tabela 1.7 + `writable_dirs()` por adapter + teste de container que escreve nos dirs.
9. **models.dev / list dinâmico toca rede** → NetworkPolicy egress precisa whitelist (models.dev p/ opencode; openrouter.ai/dashscope/api.openai.com por worker) + fallback catálogo estático se rede negar. Risco: `/v1/models` lento/falha → cache TTL + fallback.
10. **git push + token + identidade** em cada worker → env GITHUB_TOKEN/GITLAB_TOKEN + git config no core; allowed-repos ConfigMap reusado; NetworkPolicy egress p/ forge. Sem isso o brief_driven não consegue push.
11. **Custo de manutenção (6 imagens + 6 deployments)** → mitigar com base-layer compartilhada + `deploy.py` automatizando build/scale; workers escaláveis a 0 quando não usados (só sobe o que o operador escolher por stage). **Não precisa rodar todos sempre.**
12. **Segurança/threat-model** → cada CLI roda `--yolo`/bypass num pod isolado (mesmo modelo do claude-worker, Decisão #29). Prompt-injection→exfil via canais legítimos permanece (git push em repo whitelisted). Reusar NetworkPolicy + allowed-repos + audit. Não regride a postura atual.
13. **Resume/threading** → só claude/codex/qwen têm resume real; aider/goose/opencode/antigravity → **fresh sempre** (adapter declara `supports_resume=False`); o brief lê `.deile-progress.md` (já existe) p/ contexto natural. Antigravity #7 (sem conv-id) reforça fresh-only.
14. **DispatchPayload tem duas formas hoje** (deile-worker Pydantic vs claude-worker request) → o cli_worker adota o contrato claude-worker-style (brief/stage/branch/cli_model/...). Unificação total fica como FU; não bloquear.
15. **Reasoning** só p/ claude/codex(`model_reasoning_effort`)/qwen(parcial) → coluna do painel desabilita onde não há; `reasoning_resolver` retorna mas adapter ignora se não suporta.

**Inconsistência corrigida no texto:** a Parte 2 dizia "tudo via OpenRouter"; o item 4/2.2 deixa claro que **codex é exceção** (OpenAI direto). E **antigravity não é via OpenRouter** (Google-locked) — corrigido para Vertex-SA-gated.

---

## PARTE 6 — Ordem de implementação recomendada (resumo executável)
1. **Fase A** (core + base + server genérico) — fundação.
2. **Fase B** (resolver + payload) — pipeline enxerga workers.
3. **Fase C** (opencode piloto E2E) — prova o framework inteiro com 1 worker barato (OpenRouter→DeepSeek).
4. **Fase D** (aider, goose, qwen, codex) — replicar, 1 por vez, cada um com E2E.
5. **Fase E** (antigravity) — só após spike de auth passar; senão pular.
6. **Fase F** (painel + OpenRouter no DEILE + docs).
7. **Fase G** (validação full + commits por fase).

**Claude-worker:** intocado funcionalmente (só ganha o `_worker_core.py` por baixo); segue como worker premium para tarefas específicas.
