# 09 — Configuração

> Onde a configuração vive, como ela é carregada, e quais são os pontos de extensão. Catalogações em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md).

## Diretórios de configuração

> Existem **dois** diretórios `config/` distintos no repositório. Não confundir.

| Diretório | Propósito | Conteúdo |
|---|---|---|
| `config/` (raiz do repo) | Configuração runtime | `display.yaml`, `permissions.yaml`, `search.yaml`, `settings.json` |
| `deile/config/` (pacote) | Código + configs do pacote | `manager.py`, `settings.py`, YAMLs (`api_config`, `commands`, `intent_patterns`, `model_providers`, `persona_config`, `system_config`), `profiles/` |

## `Settings` (singleton, em `deile/config/settings.py`)

| Símbolo | Papel |
|---|---|
| `Settings` | Container das configurações em runtime |
| `LogLevel` | Enum de níveis de log |
| `get_settings()` | **Singleton accessor** — única forma de obter a instância |
| `update_settings(**kwargs)` | Atualiza campos in-place |
| `reset_settings()` | Reset para defaults (uso em testes) |
| `Settings.apply_overrides(d)` | Aplica dict aninhado (formato `.deile/settings.json`) sobre os campos planos |

> Regra: **nunca instanciar `Settings()` diretamente**. Sempre via `get_settings()`.

### Camadas (issue #111)

`get_settings()` lê preferências em hierarquia:

```
1. <projeto>/.deile/settings.json   (override de projeto)
2. ~/.deile/settings.json           (preferência do usuário)
3. Defaults da Settings dataclass   (fallback embutido)
```

A camada de projeto deep-merge sobre a camada do usuário (project wins em conflitos; chaves não-conflitantes coexistem). O legado `config/settings.json` continua sendo aceito como fallback **apenas** quando nenhum dos dois arquivos `.deile/settings.json` existir, com aviso de depreciação no log.

### Schema do `.deile/settings.json`

JSON aninhado por área. Apenas as chaves listadas em `_OVERRIDE_HANDLERS` são aplicadas; chaves desconhecidas são ignoradas (forward-compat). API keys NUNCA são lidas/escritas neste arquivo — secrets continuam em `.env`. Exemplo mínimo:

```json
{
  "logging":     { "level": "INFO", "to_file": true, "max_size_mb": 10, "backup_count": 5 },
  "ui":          { "streaming_enabled": true, "show_tool_details": false },
  "model":       { "default_provider": "anthropic", "max_context_tokens": 8000 },
  "caching":     { "enabled": true, "ttl_seconds": 3600 },
  "concurrency": { "max_concurrent_requests": 10, "request_timeout": 120 },
  "file_safety": { "enabled": true, "max_file_size_bytes": 1048576 },
  "deile_md":    { "enabled": true, "max_bytes": 65536 },
  "skills_paths": [],
  "environment": "development",
  "debug":       false
}
```

> Note: `skills_paths` is the only top-level array with union semantics — values from the global layer and the project layer are merged (global first, duplicates removed). All other keys follow standard project-wins-over-global layering.

### Trust-boundary (issue #125)

`<cwd>/.deile/settings.json` is **not** auto-trusted. A repo cloned from a third party can carry a settings file that disables `file_safety.enabled`, flips `debug`, or alters other security-relevant flags — the post-clone `python deile.py` would silently inherit the attacker's preferences. To prevent this, the project layer is gated by an explicit allowlist in the user's global settings:

```json
{
  "trust": {
    "project_layer_dirs": [
      "/Users/me/dev/my-trusted-repo",
      "/srv/ci/known-good-project"
    ],
    "project_layer_default": "auto"
  }
}
```

| Field | Semantics |
|---|---|
| `trust.project_layer_dirs` | List of absolute paths whose `<dir>/.deile/settings.json` is trusted as the project layer. Compared against `Path.cwd().resolve()` at boot time. |
| `trust.project_layer_default` | Migration knob with two values:<br>• `"auto"` (default) — honor non-allowlisted with a loud `WARNING` so existing CIs do not break instantly.<br>• `"deny"` — silently ignore non-allowlisted (single warning at boot). |

Default behavior in V1: `'auto'` grace period — non-allowlisted projects still apply the project layer but log a clear migration message. The next major release flips the default to `'deny'`. Operators who want strict behavior today can set `trust.project_layer_default: "deny"` in `~/.deile/settings.json`.

| Symbol | Where |
|---|---|
| `_is_project_layer_trusted(cwd, allowlist, policy)` | `deile/config/settings.py` |
| Settings fields | `Settings.trust_project_layer_dirs: List[str]`, `Settings.trust_project_layer_default: str` |
| Override key handlers | `_OVERRIDE_HANDLERS["trust.project_layer_dirs"]`, `_OVERRIDE_HANDLERS["trust.project_layer_default"]` |
| Warning text | `"settings: ignoring project layer ... not in 'trust.project_layer_dirs' allowlist"` |

> The trust boundary is read **only** from the user's global layer (`~/.deile/settings.json`). The project layer cannot allowlist itself — that would defeat the purpose.

### Settings writes are fail-closed (issue #125)

`set_setting`, `set_preference`, `add_skills_path`, and `remove_skills_path` route through `PermissionManager.check_permission` before touching disk. The default rule registered in `permissions.py:_load_default_rules` (`settings_write_default`) is `PermissionLevel.READ` — i.e. **deny write**. This matches the security-first principle in `03-PRINCIPIOS-ARQUITETURAIS.md` §5: a missing operator policy must not silently grant write access to security-relevant configuration.

To enable interactive writes (the `/settings`, `/skills add`, `--set` paths), add a policy override to `config/permissions.yaml`:

```yaml
permission_rules:
  - id: settings_write_interactive
    name: Settings Write (Interactive)
    description: Allow operator-initiated settings writes
    resource_type: file
    resource_pattern: '^settings:(global|project):.*$'
    tool_names: [settings_manager]
    permission_level: write
    priority: 40   # lower than the default rule's 50 so this wins
```

Without this rule, every write attempt logs `permission denied` to the audit (`SECURITY_POLICY_CHANGED`, `result="denied"`) and the calling command surfaces the failure to the user. To go even tighter, narrow the regex (e.g. `^settings:global:.*$` to forbid project-scope writes) or restrict the tool name. To go fully open (not recommended), keep `priority: 40` and `permission_level: write` — the operator owns this risk explicitly.

### Type-safety of legacy `config/settings.json` (issue #125 P1-4)

`Settings.load_from_file` is the legacy fallback path used when neither `~/.deile/settings.json` nor `<cwd>/.deile/settings.json` exists. As of issue #125 patch (review feedback), it now applies the converters from `_OVERRIDE_HANDLERS` to every value it accepts — not just filtering the key allowlist. Strings like `enable_file_safety_checks: "yes-please"` or `trust_project_layer_dirs: "/single"` are rejected with a warning instead of silently colliding with the typed dataclass fields.

## `ConfigManager` (config estruturada com hot-reload, em `deile/config/manager.py`)

Configura múltiplas seções tipadas:

| Símbolo | Papel |
|---|---|
| `GeminiConfig` | Configuração legada de Gemini |
| `SystemConfig` | Toggles do sistema |
| `UIConfig` | Configuração de UI |
| `AgentConfig` | Configuração do agente |
| `CommandConfig` | Configuração de comandos |
| `DeileConfig` | Agrega todas as anteriores |
| `FunctionCallingMode` | Enum de modos de function calling |

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_config_manager()` |
| Hot-reload | Via `watchdog` (lazy import) |
| Hot-reload sem watchdog | Silenciosamente desativado com aviso no log |

## YAMLs em `deile/config/`

| Arquivo | Responsabilidade |
|---|---|
| `system_config.yaml` | Toggles do agente, log level, autodiscovery, sessão |
| `api_config.yaml` | `default_model` (formato `provider:model_id` ou `null` para tier auto), config legada de Gemini (generation_config, safety_settings, tool_config) |
| `model_providers.yaml` | **Catálogo definitivo** de providers, modelos, tiers, políticas, circuit breaker, budget, feature flags. Ver [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md) |
| `intent_patterns.yaml` | Catálogo de padrões de intent para o `IntentAnalyzer`, com keywords, regex, threshold de complexidade, requisito de workflow |
| `persona_config.yaml` | Persona padrão, hot-reload, configs por persona (capacidades, modelo, comportamento, ferramentas preferidas) |
| `commands.yaml` | Configurações estendidas de comandos slash |

## Profiles (em `deile/config/profiles/`)

| Arquivo | Aplicação |
|---|---|
| `autonomous_agent.yaml` | Profile default (aplicado por `_apply_profile_layer` em `settings.py`) |
| `enterprise.yaml` | Profile opt-in (aplicado por `_apply_profile_layer` em `settings.py`) |

> Profiles são a camada de **menor** prioridade; são sobrescritos por
> `~/.deile/settings.json`, pelo `<cwd>/.deile/settings.json` (se trusted)
> e por env vars. Apenas chaves listadas em `_JSON_FIELD_MAP`
> (`deile/config/settings.py`) são aplicadas — qualquer outra chave no
> YAML é silenciosamente ignorada (issue #139). Adicionar uma chave nova
> ao profile sem entrada correspondente em `_JSON_FIELD_MAP` resulta em
> configuração morta.

## Arquivos em `config/` (raiz)

> **Status (issue #111):** este diretório foi limpo. As preferências antes
> ali agora vivem em `.deile/settings.json` (ver §Camadas). Apenas
> `config/deilebot.yaml` permanece tracked (operacional do bot — carregado por `deilebot/foundation/settings.py` em `_YAML_PATH`).

`config/settings.json` continua reconhecido como **fallback de leitura**
quando nenhum `.deile/settings.json` existe — emite aviso de depreciação
e não é regravado.

## Variáveis de ambiente

> Carregadas em `deile.py` via `python-dotenv` se houver `.env` na raiz.

| Variável | Uso |
|---|---|
| `ANTHROPIC_API_KEY` | Habilita provider Anthropic |
| `OPENAI_API_KEY` | Habilita provider OpenAI |
| `DEEPSEEK_API_KEY` | Habilita provider DeepSeek |
| `GOOGLE_API_KEY` | Habilita provider Gemini |
| `DEILE_BOT_ENDPOINT` | URL do daemon `deilebot` (control-plane HTTP). Sem isto, tools `messaging.discord_*` não registram |
| `DEILE_BOT_AUTH_TOKEN` | Bearer token do control-plane do daemon. Mesmo valor configurado nos dois lados |
| `DEILE_BOT_TIMEOUT_S` | Timeout (segundos) das chamadas do client. Default `10` |
| `DEILE_BOT_DEFAULT_GUILD_ID` | Guild Discord default (informativo, opcional) |

> Pelo menos uma das chaves de provider LLM deve estar definida para a CLI iniciar. Caso contrário, mensagem de erro listando todas as opções e saída sem subir o agente.

> As variáveis `DEILE_BOT_*` são opcionais: ausentes, a integração com o daemon fica desligada e as tools `messaging.discord_*` não aparecem na descoberta automática (sem warnings).

### Extra opcional `bot`

Para habilitar a mensageria proativa, instale o cliente:

```bash
pip install deile[bot]              # instala deilebot (apenas httpx + pydantic)
```

O daemon em si vive em `elimarcavalli/deilebot` e tem extras próprios (`discord`, `telegram`, etc.). Ver `deilebot/pyproject.toml`.

### Pipeline + Cron — variáveis de ambiente

> Todas opcionais. Ausentes, o pipeline e o cron simplesmente não iniciam automaticamente.

| Variável | Uso | Default |
|---|---|---|
| `DEILE_PIPELINE_REPO` | **Removida do código de domínio** (issue #612). Sobrevive só nos manifests como alvo do clone inicial; `Settings` a ignora — o `deile-monitor` lê o repo pelo resolver canônico, não por esta var | (sem default) |
| `DEILE_FORGE_REPO` | Project path do forge ativo (`owner/repo` GH ou `group/(subgroup/)*project` GL) — Decisão #41. **Sem default hardcoded:** `resolve_forge_repo()` aborta com `ConfigurationError` quando ausente no caminho de produção (issue #612, fail-loud), degradando com WARNING só nas surfaces graciosas (painel/CLI). Fonte única no ConfigMap `deile-runtime-config` chave `pipeline.repo` | (vazio — falha alto se não configurado) |
| `DEILE_FORGE_KIND` | `github`\|`gitlab`\|`auto` (default `auto`: detecta por URL host → path heuristic) — Decisão #41 | `auto` |
| `DEILE_GITHUB_HOST` | Hosts GitHub adicionais (CSV; ex.: `ghe.empresa.com`). `github.com` é sempre aceito | `github.com` |
| `DEILE_GITLAB_HOST` | Hosts GitLab adicionais (CSV; ex.: `gitlab.empresa.com`). `gitlab.com` é sempre aceito | `gitlab.com` |
| `DEILE_FORGE_PROBE` | Habilita HTTP probe opt-in para detectar forge em hosts desconhecidos | `false` |
| `DEILE_FORGE_BOT_LOGIN` | Handle do bot que o pipeline observa nos mentions (`@deile-one`) | `@deile-one` |
| `GITLAB_TOKEN` / `GL_TOKEN` | PAT GitLab (escopos `api`, `read_repository`, `write_repository`) | nenhum |
| `DEILE_PIPELINE_BASE_PATH` | Caminho absoluto da raiz do repositório onde `.worktrees/` será criado | Detectado automaticamente (busca ancestral com `.git` + `deile.py`) |
| `DEILE_PIPELINE_NOTIFY_USER_ID` | Discord snowflake para DMs de notificação de transições de estado | nenhum |
| `DEILE_PIPELINE_MONITOR_ID` | Identificador único deste monitor (1-32 chars `[a-zA-Z0-9_-]`); aparece em branch names, labels e worktree paths | `default` |
| `DEILE_PIPELINE_SHARD_INDEX` | Índice do shard neste monitor (int, `[0, SHARD_COUNT)`) | `0` |
| `DEILE_PIPELINE_SHARD_COUNT` | Total de shards no deploy (int `>= 1`); define quantas issues/PRs cada monitor atende por hash | `1` |
| `DEILE_PIPELINE_AUTOSTART` | Se `1`, o daemon `deilebot` inicia o `PipelineMonitor` automaticamente no boot | não setado |
| `DEILE_CRON_AUTOSTART` | Se `1`, o daemon `deilebot` inicia o `CronRunner` automaticamente no boot | não setado |
| `DEILE_CRON_DB_PATH` | Caminho absoluto do SQLite do `CronStore` | `<DEILE_PIPELINE_BASE_PATH>/data/cron.db` ou `<cwd>/data/cron.db` |
| `DEILE_PIPELINE_DISPATCH_MODE` | Estratégia de execução: `claude` (`claude -p` em worktree) ou `deile_worker` (despacha ao Pod `deile-worker` por HTTP) — Decisão #31 | `deile_worker` |
| `DEILE_PIPELINE_MAX_PARALLEL` | Teto de dispatches simultâneos por tick (gate de concorrência do monitor). Aceita inteiro ≥ 1 ou sentinel `auto` (lê réplicas do `claude-worker` via `kubectl`). Ver [§Gate de dispatch](#gate-de-dispatch-deile_pipeline_max_parallel-e-_count_total_in_flight) abaixo | `2` |
| `DEILE_PIPELINE_RESUME_ENABLED` | Master switch do resume de trabalho parcial (Decisão #30); só ativa no caminho `deile_worker` | `true` |
| `DEILE_PIPELINE_RESUME_INTERVAL` | Segundos mínimos entre tentativas de resume do mesmo item (`0` = imediato) | `0` |
| `DEILE_PIPELINE_RESUME_MAX_ATTEMPTS` | Teto de tentativas por item antes do fluxo de bloqueio (`>= 1`) | `10` |
| `DEILE_PIPELINE_RESUME_BUDGET` | Teto de wall-clock acumulado (s) entre tentativas (`0` = sem teto) | `0` |
| `DEILE_PREFERRED_MODEL` | Modelo preferido (soft) — usado para fixar o worker num modelo (ex.: `deepseek:deepseek-v4-pro`) | nenhum |
| `DEILE_REASONING_EFFORT` | Esforço de raciocínio global (soft) — `low\|medium\|high\|xhigh\|max\|ultracode\|auto` p/ anthropic/claude; específico por provider no deile-worker. Lido pelo DEILE CLI e como fallback do pipeline. Ver `deile/core/models/reasoning.py` | nenhum |
| `DEILE_PIPELINE_REASONING_<STAGE>` | Esforço de raciocínio por etapa (`classify`/`refine`/`implement`/`pr_review`/`follow_ups`) — repassado em `DispatchPayload.preferred_reasoning`; provider traduz, claude-worker → `claude --effort` | herda `DEILE_REASONING_EFFORT` |

> Estas variáveis mapeiam para chaves em `~/.deile/settings.json` (`pipeline.dispatch_mode`, `pipeline.resume_*`, `model.preferred`); usar as env vars ainda funciona mas emite *deprecation warning* pedindo para mover ao `settings.json`. Defaults a nível de `PipelineConfig` (não env): `mention_handle` (`@deile-one`) e `enable_review_human_prs` (`false` — se `true`, triagem/review reivindicam PRs de branch alheio; ver Decisões #32/#33).

> O `pipeline_tool.py` e o `pipeline_command.py` leem essas variáveis diretamente via `os.environ` (pois são componentes de borda — não domínio); isso está alinhado com a regra "adapters podem ler env, core não pode".

### Gate de dispatch: `DEILE_PIPELINE_MAX_PARALLEL` e `_count_total_in_flight`

> Referência canônica para diagnosticar stalls de backlog (issue #585). Nenhum arquivo `.py` de runtime é alterado aqui — esta subseção é documentação pura.

#### O que `DEILE_PIPELINE_MAX_PARALLEL` controla

`DEILE_PIPELINE_MAX_PARALLEL` define o **teto de dispatches simultâneos** que o monitor pode manter em voo a cada tick. O valor é lido em `deile/orchestration/pipeline/monitor.py:236–242` (`build_default_pipeline_config`) e armazenado em `PipelineConfig.max_parallel` (`monitor.py:148`).

| Valor | Semântica |
|---|---|
| inteiro ≥ 1 | limite fixo de slots concorrentes |
| `auto` | lê o número corrente de réplicas do `claude-worker` via `kubectl` (`monitor.py:169–211`); cai em `2` se `kubectl` não estiver disponível |

Default: **`2`**. Persistência: `DEILE_PIPELINE_MAX_PARALLEL` (env) → `settings.pipeline_max_parallel` → `PipelineConfig.max_parallel`. A chave `settings.json` equivalente é `pipeline.max_parallel`.

#### Como `_count_total_in_flight` é calculado

A função `_count_total_in_flight` (`deile/orchestration/pipeline/stages.py:1819`) contabiliza todo o trabalho em andamento que pertence a este monitor:

1. **Issues em estado-lock** — labels `~workflow:em_revisao`, `~workflow:em_refinamento`, `~workflow:em_arquitetura` e `~workflow:em_implementacao`, **excluindo** aquelas que também carregam `~workflow:bloqueada`, `~workflow:em_pr` ou `~workflow:aguardando_stakeholder` (estados _parked_ que não consomem worker ativo — `stages.py:1849–1863`).
2. **PRs em review** — label `~review:em_andamento` sem `~workflow:bloqueada` (`stages.py:1864–1874`).

Apenas itens **de posse deste monitor** (label `~by:<id>` ou `_this_monitor_owns`) são contados.

A regra de disponibilidade, aplicada nos despachadores de crítica (`stages.py:887`), refinamento (`stages.py:1207`) e implementação (`stages.py:2010`):

```
available = max(0, max_parallel − in_flight)
```

Quando `available ≤ 0`, o tick **silenciosamente pula** todos os novos claims daquele stage — sem erro, sem `WARNING`; apenas uma linha `logger.debug`.

> Nota: o despachador de implementação usa `_count_in_flight_issues` (`stages.py:1878`) em vez de `_count_total_in_flight` — conta somente issues em `~workflow:em_implementacao`, não PRs em review. A fórmula `max_parallel − in_flight` é a mesma.

#### Sintoma de stall e ponto de ajuste

**Sintoma:** `dispatched=0` por vários ticks consecutivos com backlog não-vazio. Os logs do pod `deile-pipeline` mostrarão linhas como:

```
critique: todos os 2 slots ocupados (2 em voo); skip novos claims
```

**Causas comuns:**
- `max_parallel` abaixo do `in_flight` real — por exemplo, issues presas em `aguardando_stakeholder` sem o label `~workflow:bloqueada`, contando incorretamente como slots ocupados.
- Número de réplicas do `claude-worker` aumentado mas `DEILE_PIPELINE_MAX_PARALLEL` não acompanhou.

**Ponto de ajuste — sem rebuild necessário:**

1. **Painel TUI** → tecla `[p]` na view de pipeline → edita `max_parallel` em tempo real (grava via `kubectl set env`).
2. **`kubectl set env`** direto:
   ```bash
   kubectl set env deployment/deile-pipeline \
     -n deile DEILE_PIPELINE_MAX_PARALLEL=4
   kubectl rollout status deployment/deile-pipeline -n deile
   ```

O monitor relê a variável no próximo boot do pod. Nenhuma reconstrução de imagem é necessária.

### Frota multi-CLI — config por etapa e do worker genérico (Decisão #51)

> Categoria de configuração da frota de CLI workers (opencode/codex/qwen/aider/goose). Referência canônica de cada variável (descrição, default, formato): [`.env.example`](../../.env.example) seções 1, 4 e 5. Esta seção descreve **responsabilidades** e onde cada chave é consumida — não duplica defaults.

A frota acrescenta os CLI workers como novos alvos de dispatch além do `deile-worker` e do `claude-worker`. Três eixos de configuração são resolvidos **por etapa** do pipeline (`classify`/`refine`/`implement`/`pr_review`/`follow_ups`), cada um por um resolver dedicado em `deile/orchestration/pipeline/`:

| Eixo | Env per-stage → global | Resolver | Consumo |
|---|---|---|---|
| **Worker (dispatch)** | `DEILE_PIPELINE_DISPATCH_<STAGE>` → `DEILE_PIPELINE_DISPATCH_MODE` (default `deile-worker`) | `dispatch_resolver.py` | escolhe qual worker (`deile-worker`/`claude-worker`/`opencode`/`codex`/`qwen`/`aider`/`goose`) atende a etapa; os kinds válidos vêm do `ADAPTERS` (auto-discovery em `cli_adapters/__init__.py`) |
| **Modelo** | `DEILE_PIPELINE_MODEL_<STAGE>` → `DEILE_PREFERRED_MODEL` | `model_resolver.py` (`resolve_stage_cli_model`) | id `provider:modelo` propagado em `DispatchPayload.preferred_model`; CLI workers usam o slug que o adapter aceita (ex.: `openrouter:vendor/model`) |
| **Reasoning** | `DEILE_PIPELINE_REASONING_<STAGE>` → `DEILE_REASONING_EFFORT` | `reasoning_resolver.py` | esforço repassado em `DispatchPayload.preferred_reasoning`; ver `deile/core/models/reasoning.py` |

Os três resolvers leem via `get_settings()` (reasoning) ou diretamente do env de borda (dispatch/model), com o respectivo handler em `settings.py` (`_OVERRIDE_HANDLERS` / `_JSON_FIELD_MAP`) — chaves `pipeline.dispatchers.<stage>`, `pipeline.models.<stage>` e `pipeline.reasoning.<stage>` no `settings.json`.

O servidor genérico dos CLI workers (`infra/k8s/cli_worker_server.py`, sobre `infra/k8s/_worker_core.py`) lê suas próprias variáveis de runtime — `DEILE_CLI_WORKER_KIND` (kind do pod), `DEILE_CLI_WORKER_HOST`/`PORT` (porta default = `adapter.default_port`), `DEILE_CLI_WORKER_ROOT`/`HOME`, `DEILE_CLI_WORKER_TASK_TIMEOUT_S`, e os tunables de cleanup/custo (`DEILE_CLI_WORKER_CLEANUP_INTERVAL_S`, `_CLEANUP_RETENTION_DAYS`, `_PROGRESS_RETENTION_DAYS`, `_PROGRESS_GRACE_S`, `_COST_LEDGER_PATH`). O OAuth opt-in por kind viaja em `DEILE_<KIND>_AUTH=oauth` (ex.: `DEILE_CODEX_AUTH`), normalmente escrito no Deployment pelo `deploy.py k8s cli-worker-login <kind>`. O bearer dos workers **não** é env: é lido do Secret file `/run/secrets/cli-worker/CLI_WORKER_BEARER_TOKEN`. Goose tem o tunable próprio `DEILE_GOOSE_MAX_TURNS` (adapter `cli_adapters/goose.py`).

**Persistência (dois caminhos, como nas Decisões #41/#47):** no **cluster**, os per-stage `DEILE_PIPELINE_{DISPATCH,MODEL,REASONING}_<STAGE>` viram env vars no Deployment via `kubectl set env` (escritos pelo painel TUI `[d]` → `DispatchMatrixView`); no **CLI local**, as mesmas chaves moram em `~/.deile/settings.json` (`pipeline.dispatchers.<stage>`/`pipeline.models.<stage>`/`pipeline.reasoning.<stage>`). Etapas sem override caem no global correspondente.

## Hot-reload

| Componente | Como funciona |
|---|---|
| Configuração estruturada | `ConfigManager` com `watchdog.Observer` e `FileSystemEventHandler` interno (`UnifiedConfigChangeHandler`) |
| Plugins | `deile/plugins/hot_loader.py:PluginFileHandler` (também via `watchdog`) |
| Personas | `PersonaManager.initialize(enable_hot_reload=True)` |

## Logging

| Aspecto | Detalhe |
|---|---|
| Accessor padrão | `deile/storage/logs.py:get_logger()` |
| Debug detalhado | `deile/storage/debug_logger.py` |
| Logging global | A CLI desabilita logging global no início (`logging.disable()`) — só os caminhos com `get_logger()` continuam ativos |

## Regras inegociáveis

| Regra | Detalhe |
|---|---|
| Acessor único | Toda leitura passa por `get_settings()` ou `get_config_manager()` — **nunca** ler `os.environ` ou YAML em código de domínio |
| Schema | Configurações novas via Pydantic ou dataclass; validação no carregamento |
| Não confundir | `./config/` (raiz) ≠ `./deile/config/` (pacote) |
| Documentar fonte | Se adicionar uma flag, documentar a fonte (qual YAML, em que seção) — datas e commits ficam no `git log` |
