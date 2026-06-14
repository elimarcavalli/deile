# Changelog

All notable changes to the DEILE project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.1.0] - 2026-06-13 — DEILE-One (frota multi-CLI + endurecimento de produção)

> Sucede a `1.0.0` (linha de base clássica). Entrega a **frota multi-CLI** plugável,
> fecha os gaps de produção da frota (custo central, allowlist enforçada, baseline de
> testes limpo), completa os três sinais OTLP do dispatch (traces + logs + métricas),
> migra a auth do `claude-worker` para token de ~1 ano, e faz uma rodada de auditoria
> de endurecimento nos subsistemas plugáveis (tools/commands/parsers/memory/storage).

### Added
- **Frota de CLI workers plugáveis (Decisão #51, #614)** — além de `deile-worker` e `claude-worker`, qualquer CLI de codificação vira um worker despachável escrevendo **um adapter** (`infra/k8s/cli_adapters/<kind>.py`) que satisfaz o Protocol `CliAdapter` (`infra/k8s/cli_adapters/base.py`). O auto-discovery em `cli_adapters/__init__.py` monta `ADAPTERS = {kind: adapter}` como **fonte única**, que dirige `dispatch_resolver`, painel e geração de manifest/NetworkPolicy — adicionar worker não edita nenhum consumidor.
- **Server genérico de worker (#614)** — `infra/k8s/cli_worker_server.py` reusa `infra/k8s/_worker_core.py` (lease/heartbeat/subprocess one-shot/HTTP bearer/cleanup/gate pós-run de commit+push+test). Endpoints: `GET /v1/health`, `GET /v1/models`, `POST /v1/dispatch`, `GET /v1/progress/{task_id}`, `GET /v1/dispatches/{task_id}/resume-info`. O gate de sucesso é `parse_output().ok AND wrapper_gate()` — o exit-code do CLI não é confiável.
- **Roteamento per-estágio (#614)** — `deile/orchestration/pipeline/dispatch_resolver.py` resolve o worker de cada estágio (`classify`/`refine`/`implement`/`pr_review`/`follow_ups`) via `DEILE_PIPELINE_DISPATCH_<STAGE>` > global `DEILE_PIPELINE_DISPATCH_MODE` (default `deile-worker`); `get_valid_dispatchers()` deriva a lista válida do registro de adapters. Modelo/reasoning per-estágio via `DEILE_PIPELINE_MODEL_<STAGE>` / `DEILE_PIPELINE_REASONING_<STAGE>`.
- **Resume nativo por worker, anti-sangria (#614)** — cada worker retoma a sessão nativa no mesmo workdir em vez de re-gastar tokens; erro de provider (402/429/insufficient) é classificado INCOMPLETO (`_worker_core.classify_provider_error`) para o pipeline retomar. Workers com `supports_resume=True` ganham PVC por worker (`<kind>-worker-home`) + CronJob de cleanup.
- **Custo durável por-PVC + auditoria de frota (#614)** — `cli_worker_server` colhe o custo de cada sessão para um ledger durável (`<root>/.cost-ledger.jsonl`, dedup por `task_id`) antes de podar o log volumoso; `infra/k8s/jsonl_cost.py` é a fonte única de preço, `infra/k8s/fleet_progress_parse.py` a dos parsers de `.progress` por kind, e `infra/k8s/fleet_tokens_audit.py` (tela `[T]okens`) agrega tokens/custo por worker × modelo.
- **Scale-to-zero on-demand (#614)** — workers nascem `replicas:0` (custo zero ocioso); `cli_worker_scaler.py` escala 0→1 sob demanda com cooldown. Gerador `infra/k8s/_cli_worker_gen.py` + template `infra/k8s/manifests/templates/cli-worker.yaml.tmpl` (manifests gerados são efêmeros/gitignored).
- **Verbos novos do `deploy.py` (#614)** — `k8s build-cli-workers [--kind <k>]` (imagem via `Dockerfile.cli-worker` multi-stage), `k8s gen-worker <kind>`, `k8s cli-worker-install <kind>`, `k8s cli-worker-login <kind>`, `k8s cli-worker-uninstall <kind>`.
- **Bloco `usage` estruturado no `/v1/dispatch` (#638)** — `WorkResult` ganha `tokens_by_model`/`model`, preenchidos server-side pelo parser único `fleet_progress_parse`; o resume-info também expõe o uso.
- **Custo central da frota no `UsageRepository` (#638)** — novo `deile/orchestration/pipeline/fleet_cost_recorder.py`: o pipeline (componente longevo) faz PUSH de 1 registro por modelo no SQLite central (caminho `wait` direto; fire-and-forget capturado no reconcile via resume-info, dedup por task_id). Sobrevive ao scale-to-zero/`force-delete`, que o ledger por-PVC não sobrevivia; a tela `[T]okens` lê o store central primeiro.
- **Métricas OTLP do dispatch (#455)** — `deile/observability/dispatch_metrics.py` (MeterProvider isolado, kill-switch, drop counter throttled): `deile.dispatch.total`/`.failed.total`/`.duration_ms`/`.tool_burst.total`, `deile.forge.pr_review.total`, `deile.git.push.total` — todas com labels de cardinalidade limitada. Completa a trinca traces (#443) + logs (#454) + métricas.
- **Propagação W3C traceparent cross-pod (#457)** — `deile.dispatch` vira filho do span `pipeline.dispatch_request` (mesmo trace_id) pela injeção/extração de `traceparent` pipeline→worker, com fallback a span raiz quando ausente.
- **Atributos SemConv `vcs.*` dual-emitidos (#456)** — `semconv_mapping.apply_semconv_attrs` mapeia attrs de `git.*`/`forge.*` para `vcs.ref.head.name`/`vcs.repository.url`/`vcs.change.id`/`vcs.change.state` nos child spans, sob toggle `DEILE_OTLP_SEMCONV_ENABLED` (default on).
- **Activity sources configuráveis (#447)** — `settings.panel.activity_sources` permite escolher quais deployments o widget ACTIVITY acompanha (lista/role/cor/ordem) sem editar Python nem re-deployar; valida DNS-1123 e rejeita duplicatas.
- **Controles destrutivos na LiveSessionView (#462)** — `[k]` kill e `[C]` cleanup com confirmação inline de 2-keypress, defesa TOCTOU no servidor (409) e audit `{allowed, failed, cancelled}`.
- **Auth do `claude-worker` via `claude setup-token` (#603, Decisão #52)** — verb `deploy.py k8s claude-setup-token` + hotkey `[T]` no painel; token de ~1 ano em `CLAUDE_CODE_OAUTH_TOKEN` injetado por env var via Secret K8s.

### Changed
- **Painel TUI** — `DispatchMatrixView` (`[d]`) passa a matriz de estágios × {Worker, Model, Reasoning}; tela `[T]okens` vira auditoria da frota.
- **Auth do claude-worker migrada (#603)** — remove o OAuth de ~8h (credentials.json + flock + initContainer `bootstrap-creds` + CronJob de renovação 4h) em favor do token de ~1 ano. `claude-login`/`claude-renew` ficam **DEPRECATED**. Billing: a partir de 15/jun/2026, uso via `claude -p` em planos de assinatura consome crédito mensal de Agent SDK separado do interativo.
- **Endurecimento server-side do `deile-worker` (#620)** — graceful shutdown (SIGTERM dreva tasks com timeout, 503 durante shutdown, hard-deadline `os._exit`), métricas, idempotência, rate-limit, validação de schema; client-side retry + circuit breaker (AC4/AC5).
- **Definition-of-Done do implement gateada por evidência de AC (#609)** — briefs confrontam entrega vs ACs; skip de `@integration` não conta como verde; spikes usam `Refs` (não `Closes`); `mark_draft_cmd` branch-keyed nos dois forges.
- **Repo do forge project-agnostic, fail-loud (#612)** — `resolve_forge_repo()` aborta com `ConfigurationError` se não configurado (em vez de cair no hardcoded `elimarcavalli/deile`); repo-alvo vem do ConfigMap `deile-runtime-config` chave `pipeline.repo`. `deile-monitor` passa a ler o repo pelo resolver canônico, não por `DEILE_PIPELINE_REPO`.
- **Refator DRY (#643)** — helpers compartilhados em `cli_adapters/base.py`, catálogo OpenRouter em `_catalog.py`, helpers kubectl, engine de cost-ledger em `_worker_core.py`, single-source de `PIPELINE_STAGES` + aliases de dispatch.
- **Auditoria de endurecimento dos subsistemas plugáveis** — slash commands (#657, incl. timeout defensivo em `ensure_gh_authenticated`), `except Exception: pass` → DEBUG-then-suppress (#656), schema inline para `python_execute`/`pip_install` (#651), `run_tests` function-callable (#652), rede de regressão para os parsers (#659).
- **Painel `[A]` restaura a ActionsView e conserta o focus trap da Activity (#667)**.
- **`fix(panel)`: kill-409 auditado como `allowed` (#678)** — alinha ao cleanup-409 (ação despachada ao servidor é allowed; `failed` só p/ timeout/conn/5xx).

### Fixed
- **Custo Gemini gravado como zero em silêncio (#661)** — `GeminiProvider._compute_cost` não existia (nome certo: `estimate_cost`); o `AttributeError` era engolido pelo fail-open, zerando o custo de toda request Gemini. Implementado + WARNING em cost=0 com tokens faturados (#665).
- **TOCTOU no cleanup de workdir de pod morto (#649)** — o guard de #520 re-admitia o workdir olhando só a idade do heartbeat, ignorando o registro de presença (#495).
- **Guards de redispatch de reviewer + cap de concorrência (#668)** — menção de reviewer gateada por `~mention:processado`; honra `~workflow:bloqueada` no filtro de candidatos; cap de reviews por `max_parallel`; cap global de claude concorrentes via contagem de leases vivos no PVC (cross-pod), substituindo a frágil soma-de-labels.
- **Cost-cap global via settings.json estava morto (#666)** — `resolve_stage_cost_cap_usd` nível 4 lia campo inexistente; campo adicionado. `get_config_manager()` ganha lock + double-checked locking (Decisão #11).
- **`ProceduralMemory` fazia I/O JSON síncrono no event loop (#663)** — roteado via `asyncio.to_thread`; tolera `patterns.json` corrompido no `initialize()` (#662).
- **3 testes defasados em `orchestration/pipeline` (#642, fecha #640)** — alinhados à extensão do reaper (#427); nenhum código de produto alterado.
- **`fix(docker)`: COPY de `_worker_core.py` para `/app`** (módulo novo da frota faltava na imagem do claude-worker).
- Cobertura de regressão: gaps de export (#461), timeout em `ensure_gh_authenticated` (#655), skip de testes de kill-switch sem o extra `[otel]`.

### Security
- **Allowlist de repos enforçada por request, antes do clone (#639)** — `_worker_core.check_repo_allowed` retorna 403 `REPO_NOT_ALLOWED` nos **dois** servidores de dispatch, fechando o gap onde só havia fail-fast no startup do `wrapper.py` (vetor de exfiltração por prompt-injection). Egress ainda não é host-whitelisted em L3/L4 — a allowlist é o controle de aplicação.
- **Strip real de `ANTHROPIC_API_KEY`/`ANTHROPIC_AUTH_TOKEN` do subprocess do `claude -p` (#603)** — antes estava só na docstring; venciam o `CLAUDE_CODE_OAUTH_TOKEN` na precedência e cobravam via API. Guard anti-`--bare` (bare mode não lê o token).

### Dependencies
- Bumps: `idna` 3.17→3.18 (#635), `tqdm` 4.67.3→4.68.1 (#636), `wcwidth` 0.7.0→0.8.1 (#637).

## [1.0.0] - 2026-06-08

Primeiro release oficial do DEILE. Marca a linha de base **clássica** do agente
autônomo de desenvolvimento em modo CLI — pipeline de issues/PRs/menções, memória
de quatro camadas, multi-provider LLM e a stack Kubernetes (deile-worker +
claude-worker + pipeline + bot + monitor + shell) — imediatamente antes da frota
multi-CLI (que entra na `1.1.0`).

As entradas `[Unreleased]` abaixo documentam o trabalho consolidado neste corte.
A numeração anterior (`5.1.0`, atribuída arbitrariamente no início do projeto)
nunca foi publicada como release; `1.0.0` é o primeiro corte oficial.

## [Unreleased] — System-wide bug audit (PR #298)

### Fixed
- **Critical — `AuditLogger` crash on fresh install**: `mkdir(exist_ok=True)` lacked `parents=True`; default `~/.deile/logs` failed when `~/.deile` didn't exist, blocking the whole security module from loading. (`deile/security/audit_logger.py`)
- **Critical — Hot-reload de plugins morto**: `PluginFileHandler.on_modified` rodava no thread do `watchdog.Observer`; `asyncio.create_task` lançava `RuntimeError` silenciosamente. `HotLoader.start` agora captura o loop e usa `run_coroutine_threadsafe`. (`deile/plugins/hot_loader.py`)
- **High — `PlanManager` step timeout não-funcional**: `_run_tool_with_params` era `async def` sem `await`, delegava ao bridge síncrono que bloqueava o loop em `Future.result()` — `asyncio.wait_for(timeout=step.timeout)` perdia o budget. Agora invoca `tool.execute()` direto (com validação de schema preservada). (`deile/orchestration/plan_manager.py`)
- **High — `ToolResult` attribute access broken em PlanManager/WorkflowExecutor**: referências a `.success`/`.output`/`.error_message` (que não existem em `ToolResult` — usar `is_success`/`data`/`message`); plans nunca completavam um step. (`deile/orchestration/plan_manager.py`, `deile/orchestration/workflow_executor.py`)
- **High — `stop_on_failure` ignorado**: `break` saía só do loop interno; o `while True` externo continuava. Agora também marca `_stop_flags[plan.id]`. (`deile/orchestration/plan_manager.py`)
- **High — OpenAI/DeepSeek cost double-counted cached tokens**: `prompt_tokens` da OpenAI inclui o subset cached; a fórmula base cobrava ambos. Override em `OpenAIProvider.estimate_cost` (herdado pelo DeepSeek). (`deile/core/models/openai_provider.py`, `deepseek_provider.py`)
- **High — Fire-and-forget tasks GC-able**: `MemoryManager` e `agent.py` usavam `asyncio.create_task(...)` sem ref forte; o loop só mantém weakref. Agora `MemoryManager._spawn_background` mantém um `Set[Task]`; `agent._publish_tool_event` faz `await` direto. (`deile/memory/memory_manager.py`, `deile/core/agent.py`)
- **High — `CircuitBreaker.is_open` mutava state**: transicionava OPEN→HALF_OPEN consumindo o probe slot; com duplicate provider_ids no cascade ficava preso. `is_open` agora é read-only; `TierRouter.select` chama `allow_request` no commit. (`deile/core/models/tier_router.py`)
- **High — `EventBus.publish_and_wait` stub retornando True**: `_is_event_processed` era hardcoded `return True`. Tracker FIFO bounded (10k) registra event_ids ao final de `_process_event`. (`deile/events/event_bus.py`)
- **High — EventBus wildcard subscription leak no worker**: `worker_server._run_task` registrava handler por dispatch sem `unsubscribe_all` (não existia). API nova + cleanup no `finally`. (`deile/events/event_bus.py`, `infra/k8s/worker_server.py`)
- **High — Sync I/O em `async def`** (princípio 03 §1): `debug_logger.log_router_event`, `approval_system._save/_load_request`, `plan_manager.load/list/save_plan`, `semantic_memory.store_knowledge`, `config/manager._persist_persona_config_change`. Todos movidos para `asyncio.to_thread` via novo helper compartilhado `deile/storage/aio_fileio.py`.
- **Medium — Gemini role `"assistant"` inválido**: SDK Google GenAI aceita só `user`/`model`. Multi-turn quebrava com 400. (`deile/core/models/gemini_provider.py`)
- **Medium — `MemoryConsolidator.consolidate_all` reportava `expired_cleaned=0` sempre**: `get_stats()` já fazia cleanup antes da chamada do consolidator. Agora captura `entries_before` antes do cleanup. (`deile/memory/memory_consolidation.py`)
- **Medium — `WorkingMemory._cleanup_loop` hot-loop em erro persistente**: sem sleep no `except`. Agora 60s de recovery sleep. (`deile/memory/working_memory.py`)
- **Medium — `bash_tool` PTY `master_fd` leak em TimeoutError**: cleanup só rodava no caminho feliz. Wrapped em try/finally + reap do subprocess. (`deile/tools/bash_tool.py`)
- **Medium — `SearchTool` perdia matches para paths fora do `cwd`**: `relative_to(Path.cwd())` lançava `ValueError` não capturado, dropava silenciosamente todas as matches do arquivo. (`deile/tools/search_tool.py`)
- **Medium — `SecretsScanner.redact_text` corrompia comprimento**: `redaction_char * len(matched_text)` (curto) substituía `[start_pos:end_pos]` (full match). Agora span-based. (`deile/security/secrets_scanner.py`)
- **Medium — `SemanticMemory.store_knowledge` mutava dict do caller**: agora copia antes de adicionar `stored_at`. (`deile/memory/semantic_memory.py`)
- **Medium — Round-robin não-atômico**: `idx = self._cursor; self._cursor += 1` permitia colisão sob `asyncio.gather`. Migrado para `itertools.count`. (`deile/core/models/routing_strategies.py`)
- **Medium — `active_requests` nunca decrementado**: contador crescia indefinidamente, quebrando `LEAST_BUSY`/`LOAD_BALANCED`. Estratégias migradas para `total_requests` (monotônico-correto). (`deile/core/models/routing_strategies.py`)
- **Medium — `infra/k8s/deploy.py` FD leak em `os.fdopen`**: falha no wrap deixava FD aberto pro arquivo de credenciais. (`infra/k8s/deploy.py`)
- **Medium — `mkdir` sem `parents=True`** em `plugins/marketplace.py` e `orchestration/approval_system.py`.
- **Medium — `relative_to` sem guard** em `tools/_file_listing.py`, `cli.py:_get_project_files`, `parsers/file_parser.py:get_suggestions` (autocomplete silenciosamente vazio).

### Added
- **`deile/storage/aio_fileio.py`** — utilitário compartilhado com `read_json` / `write_json` / `write_text` (cada um envolve `asyncio.to_thread`), usado por `orchestration/approval_system.py` e `orchestration/plan_manager.py`. Ver Decisão #34.
- **`EventBus.unsubscribe_all(handler)`** — API simétrica a `subscribe_all`; usada pelo `worker_server` no cleanup pós-dispatch.
- **`MemoryManager._spawn_background(coro, name=...)`** — helper que mantém hard reference da task em um `Set` e loga exceções não-cancelamento via `done_callback`.
- Bateria de **~70 testes de regressão novos** cobrindo cada um dos 23 bugs corrigidos, sob `deile/tests/{events,memory,plugins,orchestration,security,tools,core/models}/`.
- Validação live **`deile/tests/might/llm_validation/validate_deepseek.py`** (opt-in via `DEEPSEEK_LIVE=1`) — round-trip real cobrindo basic generate, cost-cached formula, multi-turn, streaming, e loop liveness sob PlanManager. Custo <$0.001 por execução.

## [5.1.0] — Multi-Provider Model Router

### Added
- **Multi-Provider Support**: Anthropic, OpenAI, DeepSeek providers alongside the existing Gemini integration.
- **ModelCatalog**: Immutable model registry loaded from `deile/config/model_providers.yaml` with pricing, context window, and capability data for 9+ models.
- **TierRouter + RoutingPolicy**: Tier-aware cascade routing (tier_1→flagship, tier_2→balanced, tier_3→fast, tier_4→ultra-fast) with `task_optimized` and `cost_optimized` strategies.
- **CircuitBreaker**: Per-provider consecutive-failure threshold with configurable cooldown (CLOSED→OPEN→HALF_OPEN→CLOSED).
- **UnifiedStreamEvent**: Typed streaming events (`TEXT_DELTA`, `TOOL_USE_START/END`, `USAGE_FINAL`, `ERROR`) across all providers.
- **UsageRepository**: SQLite-backed append-only usage store (`data/usage.db`) with per-session and per-provider-daily cost aggregation.
- **BudgetGuard**: Per-session and per-provider-daily spend limits with `BudgetExceeded` exception.
- **Intent tier classification**: `classify_tier(IntentAnalysisResult) → ModelTier` mapper + `tier:` field on all 11 intent patterns in YAML.
- **Prompt caching**: Anthropic `cache_control: ephemeral` on system prompt; OpenAI automatic caching extraction; DeepSeek `prompt_cache_hit_tokens`.
- **JSONL observability**: `debug_logger.log_router_event()` appends structured events to `logs/router_events.jsonl`.
- **`/model` command rewrite**: subcommands `list`, `current`, `use <provider:model>`, `use auto`, `strategy`, `cost`, `budget`.
- **Conditional bootstrap**: `bootstrap_providers()` in `deile/core/models/bootstrap.py` — registers only providers whose API key env var is set; zero providers → clear error.
- **Performance benchmarks**: router `select()` < 1ms avg; schema translation < 10ms for 10 tools.
- **Integration test stubs**: E2E tests for Anthropic and OpenAI fallback (skipped without real API keys).

### Changed
- `deile.py` entry point no longer requires `GOOGLE_API_KEY` — any of `ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `DEEPSEEK_API_KEY`, `GOOGLE_API_KEY` is sufficient.
- `GeminiProvider.chat_with_tools` now implements unified contract `(messages, tools, system_instruction) → (str, list, ModelUsage)`.
- `_generate_response_stream` in `agent.py` consumes `UnifiedStreamEvent` from all providers; legacy str-yielding providers remain backward compatible.
- `pytest.ini` section corrected from `[tool:pytest]` to `[pytest]`; `perf` and `integration` markers registered.

### Fixed
- `deile/storage/` package was blocked by `.gitignore` `storage/` rule — added `!deile/storage/` negation.
- `SandboxCommand` import in `deile/commands/builtin/__init__.py` wrapped in try/except to avoid crash when `deile.infrastructure.security` is absent.

## [Unreleased]

### Added
- **Complete DEILE 5.0 ULTRA Transformation**:
  - **GitHub Infrastructure**: CODEOWNERS, issue/PR templates, Dependabot, workflow CI/CD completa (272 linhas)
  - **Intent Analysis System**: `intent_patterns.yaml` (436 linhas) + `intent_analyzer.py` (833 linhas) + `intent_metrics.py` (657 linhas)
  - **Task Orchestration**: SQLite Task Manager (574 linhas) + Workflow Executor (404 linhas) + Task Manager base (570 linhas)
  - **Memory System**: Working Memory (458 linhas) + Persistent Memory (635 linhas) + Memory Models (229 linhas)
  - **Enhanced Personas**: Sistema BaseAutonomousPersona (915 linhas) + Developer instructions (64 linhas) + Loader com MD support
  - **Universal File Support**: Análise de arquivos binários, detecção de magic numbers, suporte a imagens/PDFs/archives
  - **Advanced Metrics**: Sistema completo de tracking de performance para intent analysis com cache e alertas
  - **Legal & Compliance**: MIT License, .gitignore abrangente (40 entradas), documentação estruturada

### Changed
- **Arquitetura Completamente Reestruturada**:
  - Versão 4.0.0 → 5.0.0 ("deile-5.0-ultra")
  - Migração de personas hardcoded para sistema dinâmico MD-based
  - Context manager aprimorado com integração de personas
  - Agent core com detecção automática de workflows via intent analyzer
  - File tools com suporte universal a arquivos (binários + texto)
  - Timeout de requests aumentado de 30s → 120s
  - Paths relativos em configurações para melhor portabilidade

### Fixed
- **Correções Críticas de Autonomia**:
  - Sistema não detectava workflows automaticamente
  - Personas não carregavam instruções dinâmicas de arquivos MD
  - Context manager não integrava corretamente com personas
  - File tools falhavam com arquivos binários ou encodings complexos
  - Clear command não estava atualizado para v5.0
  - Configurações hardcoded impediam flexibilidade

### Security
- **Melhorias Substanciais de Segurança**:
  - Audit logger expandido com logs de planos e aprovações
  - Permission manager com instância singleton segura
  - API keys nunca mais salvas em arquivos de configuração
  - Validação robusta de tamanhos de arquivo e tipos permitidos
  - Proteção contra exposição de dados sensíveis via .gitignore expandido

## [5.0.0] - 2025-09-14

### Added
- **Complete GitHub Infrastructure**:
  - CODEOWNERS file for code ownership management
  - Issue templates for bug reports and feature requests
  - Pull request template with comprehensive checklist
  - Dependabot configuration for automated dependency updates
  - Comprehensive CI/CD pipeline with multi-OS testing, security scans, and quality checks
- **New Core Modules**:
  - Intent analysis system with configurable patterns (intent_patterns.yaml)
  - Intent analyzer and metrics modules for better user input understanding
  - Advanced orchestration system with SQLite task manager and workflow executor
- **Memory & Personas System**:
  - Multi-layer memory system (working, persistent, models)
  - Dynamic persona system with developer instructions
  - Enhanced persona loader with instruction management
- **Project Documentation & Legal**:
  - MIT License
  - Comprehensive .gitignore with project-specific exclusions
- **Enhanced Configuration**:
  - Extended settings.json with new features and optimizations
  - File encoding detection and size limits
  - Improved security and safety checks

### Changed
- **Version Upgrade**: 4.0.0 → 5.0.0
- **Build Information**: Updated to "deile-5.0-ultra" (2025-09-14)
- **Configuration Improvements**:
  - Increased request timeout from 30s to 120s for better reliability
  - Updated working directories to use relative paths
  - Enhanced file handling with encoding detection
- **Core System Updates**:
  - Enhanced agent, context manager, and security modules
  - Improved UI and console interface
  - Updated file tools with better functionality
  - Enhanced clear command implementation

### Fixed
- BG001: DEILE não funcionando de forma autônoma - Corrigida captura de tool results do Chat Session response
- BG002: Sistema de personas hardcoded - Implementada integração completa do PersonaManager com system instructions dinâmicas
- BG003: Instruções hardcoded no código - Criado sistema InstructionLoader para carregar todas as instruções de arquivos MD
- BG004: Sistema não detecta intenção corretamente - Implementado sistema de self-awareness para perguntas sobre o DEILE com resposta completa e formatada

### Security
- Enhanced audit logger and permissions system
- Improved file safety checks and validation
- Added security scanning in CI/CD pipeline
- Protected sensitive files through .gitignore updates

## Development Notes

This CHANGELOG.md is automatically updated when work items are completed.
Each feature, bugfix, improvement, and major change will be documented here.