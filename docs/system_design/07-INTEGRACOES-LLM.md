# 07 — Integrações com Modelos LLM

> Multi-provider, roteamento por tier, circuit breaker, budget. Implementação em `deile/core/models/`. Catálogo concreto de providers e modelos em [`deile/config/model_providers.yaml`](../../deile/config/model_providers.yaml).

## Provedores suportados

| Provider | Provider ID | SDK | Variável de ambiente | Provider class |
|---|---|---|---|---|
| Anthropic | `anthropic` | `anthropic` (SDK oficial) | `ANTHROPIC_API_KEY` | `deile/core/models/anthropic_provider.py:AnthropicProvider` |
| OpenAI | `openai` | `openai` | `OPENAI_API_KEY` | `deile/core/models/openai_provider.py:OpenAIProvider` |
| DeepSeek | `deepseek` | `openai` (compat layer; `base_url` customizada em `model_providers.yaml`) | `DEEPSEEK_API_KEY` | `deile/core/models/deepseek_provider.py:DeepSeekProvider` (subclassa `OpenAIProvider`) |
| Gemini | `gemini` | `google-genai` (novo SDK) | `GOOGLE_API_KEY` | `deile/core/models/gemini_provider.py:GeminiProvider` |
| OpenRouter | `openrouter` | `openai` (gateway OpenAI-compatible; `base_url` customizada em `model_providers.yaml`) | `OPENROUTER_API_KEY` | `deile/core/models/openrouter_provider.py:OpenRouterProvider` (subclassa `OpenAIProvider`) |

> Pelo menos uma `*_API_KEY` precisa estar definida no ambiente. Se nenhuma estiver, a CLI sai com mensagem de erro instruindo qual variável definir.

> Os provedores acima são consumidos **in-process** (SDK chamado dentro do processo DEILE). A frota multi-CLI (abaixo) é uma camada de execução distinta — invoca o LLM por subprocess de CLIs de coding, cada um com seu próprio provider/auth.

## Frota multi-CLI — execução de LLM por subprocess (Decisão #51)

Além dos providers in-process, DEILE integra LLMs por uma **camada de execução** distinta: a frota de CLI workers. Cada worker é um pod que executa, em subprocess one-shot dentro de um worktree isolado, um CLI de coding headless — o CLI fala com o seu próprio LLM/provider, sem passar pelos `ModelProvider` deste pilar. Ver Decisão #51 (`00-VISAO-GERAL.md` / `DECISOES.md`) para o desenho; o roteamento per-stage que escolhe qual worker recebe cada etapa vive em [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md).

> Não enumere os workers aqui — o registro de adapters é a fonte única (`ls infra/k8s/cli_adapters/*.py`).

### Adapters e responsabilidades

| Responsabilidade | Onde vive |
|---|---|
| Contrato do adapter por CLI (Protocol `CliAdapter` + metadados `kind`/`default_port`/`auth_mode`/`supports_resume`/`git_strategy`/`auth_env_keys`/`egress_hosts` + métodos `build_argv`/`env_overlay`/`parse_output`/`list_models`/`extract_session_id`/`provision_auth`) | `infra/k8s/cli_adapters/base.py` |
| Adapter concreto por CLI (encapsula o CLI + seu provider/auth) | `infra/k8s/cli_adapters/<kind>.py` |
| Server genérico que reusa `_worker_core` (lease/heartbeat/subprocess/HTTP bearer/cleanup) e delega ao adapter os pontos divergentes | `infra/k8s/cli_worker_server.py` |

### Auth por worker

Dois modos declarados pelo adapter (`AuthMode` em `cli_adapters/base.py:41`):

| Modo | Detalhe |
|---|---|
| `env` | Chave de API via variável de ambiente — não expira; default recomendado para automação. Chaves no Secret `cli-worker-keys` |
| `oauth_file` | Credencial OAuth montada num arquivo; opt-in via `DEILE_<KIND>_AUTH=oauth`; exige bootstrap + refresh in-pod (mecanismo generalizado do `claude-login` via `OAuthSpec`) |

Cada adapter declara as `auth_env_keys` que o seu CLI exige (verificável no campo `auth_env_keys=` de cada `cli_adapters/<kind>.py`). As rotas de provider divergem por CLI:

| CLI | Rota de auth/provider (ground-truth no adapter) |
|---|---|
| codex | OpenAI **direto** (`OPENAI_API_KEY`); `wire_api="responses"` inviabiliza a maioria do OpenRouter. Dual-mode: alguns modelos exigem conta ChatGPT (OAuth `auth.json`) via `provision_auth` (`cli_adapters/codex.py`) |
| qwen | Tríade `OPENAI_*` (`OPENAI_BASE_URL`/`OPENAI_API_KEY`/`OPENAI_MODEL`) apontando para Dashscope ou OpenRouter (`cli_adapters/qwen.py`) |
| aider, goose | OpenRouter (`OPENROUTER_API_KEY`) — uma chave → vários modelos (`cli_adapters/aider.py`, `goose.py`) |
| opencode | OpenRouter (`OPENROUTER_API_KEY`) (`cli_adapters/opencode.py`) |

### Custo durável da frota

Os CLIs raramente expõem custo de forma confiável; o custo é colhido do log de progresso de cada sessão para um **ledger durável** ANTES da poda do log volumoso (espelha o claude #445):

| Responsabilidade | Onde vive |
|---|---|
| Fonte única de preço dos modelos da frota (não-claude): tabela por substring + fallback conservador + custo de um bloco de tokens | `infra/k8s/jsonl_cost.py` (`FLEET_PRICING_BY_SUBSTRING`, `fleet_pricing_for`, `fleet_cost_of_model`) |
| Preço declarado pelo adapter (`ModelInfo.price_in`/`price_out`/`cached_in`) prevalece sobre a tabela quando presente | `cli_adapters/base.py:ModelInfo` → `jsonl_cost.fleet_pricing_for(declared=...)` |
| Ledger durável append-only por-PVC (fallback local; path em `DEILE_CLI_WORKER_COST_LEDGER_PATH`, default `<root>/.cost-ledger.jsonl`, dedup por task_id) | `infra/k8s/cli_worker_server.py` |
| **Push central do custo da frota para o `UsageRepository`** (1 registro por modelo; caminho `wait` direto, fire-and-forget capturado no reconcile via resume-info; dedup por task_id) — issue #638 | `deile/orchestration/pipeline/fleet_cost_recorder.py` |
| Auditoria por worker × modelo (lê o **store central primeiro**, depois ledger dos podados + progresso vivo) | `infra/k8s/fleet_tokens_audit.py` (tela `[T]okens` do painel) |

> O custo dos providers **in-process** sempre passou pelo `UsageRepository` (seção "Budget e custo" abaixo). Desde a issue **#638 (resolvida na 1.1.0)**, o custo da frota CLI também é **empurrado centralmente** para o `UsageRepository`: o worker devolve um bloco `usage` estruturado no `/v1/dispatch` (parser único `fleet_progress_parse`) e o pipeline (componente longevo) grava 1 registro por modelo — sobrevivendo ao scale-to-zero/`force-delete`, que o ledger por-PVC não sobrevivia. O ledger por-PVC permanece como fallback local. Preço pela fonte única `jsonl_cost.fleet_cost_of_model`; escrita best-effort (falha nunca derruba o dispatch nem o tick).

## Catálogo e tiers

| Aspecto | Detalhe |
|---|---|
| Loader do catálogo | `ModelCatalog` em `deile/core/models/catalog.py` lê a seção `models:` do YAML |
| Estrutura | `ModelHandle`s imutáveis |
| Lista completa (modelos por tier, estratégias) | Vive **apenas** em [`deile/config/model_providers.yaml`](../../deile/config/model_providers.yaml) |

### Campos de `ModelHandle`

| Campo | Conteúdo |
|---|---|
| `provider_id` | Identificador do provider |
| `model_id` | Identificador do modelo |
| `tier` | Ex.: `tier_1`, `tier_2`, `tier_3`, `tier_4` |
| `label` | Livre, descritivo |
| `display_name` | Nome legível |
| `pricing` | Input/output/cached_input em USD por 1M tokens |
| `context_window` | Janela de contexto |
| `capabilities` | `function_calling`, `streaming`, `caching`, `vision`, … |

## Estratégias de roteamento

`RoutingPolicy` (em `tier_router.py`) lê `policies:` do YAML.

| Estratégia | Critério |
|---|---|
| `task_optimized` | Prioriza qualidade por tier |
| `cost_optimized` | Prioriza custo por tier |

> A estratégia default é definida em `default_strategy` no topo do YAML.

## Roteamento

> Existem dois roteadores convivendo.

### `ModelRouter` (legado, em `deile/core/models/router.py`)

Mantém compatibilidade. Operações:

| Operação | Assinatura |
|---|---|
| Registrar provider | `register_provider(provider, priority=1)` |
| Selecionar provider | `await select_provider(context)` |
| Executar com fallback | `await execute_with_fallback(...)` (tenta sequencialmente até obter sucesso ou exaurir) |
| Estatísticas | `await get_stats()` |
| Providers disponíveis | `await _get_available_providers()` |
| Aplicar estratégia | `await _apply_routing_strategy(...)` |

> `RoutingStrategy` é um enum aplicável a esse router.

### `TierRouter` (novo, em `deile/core/models/tier_router.py`)

| Aspecto | Detalhe |
|---|---|
| Acessor singleton | `get_tier_router(yaml_path=None)` |
| Composição | `ModelCatalog` (registry imutável de handles) + `RoutingPolicy` (cascata por tier por estratégia) + `CircuitBreaker` (composto por `_ProviderBreaker` por provider) |
| Cascata | O turno solicita um tier; o `TierRouter` itera pelos handles do tier na ordem da estratégia, **pulando providers em estado breaker aberto** (`BreakerState`) |
| Exceção em falha total | `NoProviderAvailable` lançada quando não há provider hígido para o tier |

### `bootstrap_providers` (em `deile/core/models/bootstrap.py`)

Lê `model_providers.yaml`:

| # | Passo |
|---|---|
| 1 | Para cada `provider_id` em `providers:`, verifica `enabled` e `api_key_env` no ambiente |
| 2 | Importa a classe do provider (mapeamento estático em `_PROVIDER_CLASSES`) |
| 3 | Para cada `ModelHandle` desse provider no catálogo, instancia uma instância dedicada |
| 4 | Registra em **ambos** os roteadores: legado por priority e tier por handle full key (`provider:model_id`) |
| 5 | Retorna a lista de `provider_id`s registrados com sucesso |

> A flag `feature_flags.use_legacy_gemini_only=true` em `model_providers.yaml` desvia o caminho para `_bootstrap_legacy_gemini` em `deile.py`, que registra **apenas** `GeminiProvider` via fluxo legado.

## Circuit breaker

Configuração em `model_providers.yaml` sob `circuit_breaker:`:

| Campo | Descrição |
|---|---|
| `consecutive_failures_threshold` | Falhas seguidas para abrir o breaker do provider |
| `cooldown_seconds` | Janela em estado aberto antes do half-open |
| `half_open_test_requests` | Quantas requisições de teste durante half-open |

| Aspecto | Implementação |
|---|---|
| Componentes | `_ProviderBreaker` + `CircuitBreaker` em `tier_router.py` |
| Registro de resultado | `DeileAgent._self_record_circuit(provider_id, success=...)` |
| Eventos | Re-emitidos via `_emit_router_event(...)` |

## Budget e custo

Modelo em `deile/storage/usage_repository.py`:

| Símbolo | Papel |
|---|---|
| `UsageRecord` | Registro de uso (provider, modelo, tokens, custo, sessão) |
| `UsageRepository` | CRUD em SQLite |
| `BudgetGuard` | Decide se a chamada estoura o orçamento (sessão / dia / mês por provider) |
| `BudgetExceeded` | Exceção lançada quando o limite é atingido |

Configuração em `model_providers.yaml` sob `budget:`:

| Campo | Descrição |
|---|---|
| `enabled` | Booleano |
| `per_session_usd` | Limite por sessão |
| `per_provider_daily_usd` | Limite diário por provider |
| `per_provider_monthly_usd` | Limite mensal por provider |
| `alert_threshold_pct` | Threshold (%) para alerta |

> O agente captura `BudgetExceeded` e emite resposta estruturada (`metadata.budget_exceeded=True`) com `provider_id` e `limit_type`.

## Function calling

Cada provider implementa o protocolo de function calling com declarações geradas a partir de `ToolSchema` (em `deile/tools/base.py`):

| Conversor | Provider de saída |
|---|---|
| `to_anthropic_tool()` | Anthropic |
| `to_openai_function()` | OpenAI / DeepSeek |
| `to_gemini_function()` | Gemini |

> A geração das listas para o turno é feita pelo `ToolRegistry` (`get_anthropic_tools`, `get_openai_functions`, `get_gemini_functions`), que filtra por autorização e `SecurityLevel`.

## Streaming

| Aspecto | Detalhe |
|---|---|
| Default na CLI interativa | Habilitado quando `Settings.streaming_enabled` é True |
| Método do agente | `process_input_stream(...)` retorna `AsyncIterator` |
| Consumidor | `ConsoleUIManager.display_streaming_turn(...)` |

| Eventos cobertos | Significado |
|---|---|
| `TEXT_DELTA` | Pedaços de texto da resposta |
| `tool_start` / `tool_end` | Lifecycle de tools |
| Eventos de roteamento de provider | Troca/queda de provider |
| `budget` | Mensagem estruturada de orçamento |
| `forced_model_not_registered` | Mensagem estruturada de modelo forçado inexistente |

## Reasoning effort — defaults opinados por stage (issue #450)

`dispatch_resolver.resolve_stage_reasoning_effort(stage)` introduz um **4º nível de fallback** entre o global settings e `None`, ativado quando operador e usuário não configuraram `reasoning_effort` explicitamente.

### Mapeamento `_STAGE_DEFAULT_REASONING_EFFORT`

| Stage | Default | Justificativa |
|---|---|---|
| `classify` | `low` | Decisão de roteamento leve; custo supera benefício de raciocínio estendido |
| `refine` | `low` | Refinamento de escopo — requer precisão, não profundidade especulativa |
| `implement` | `medium` | Geração de código se beneficia de chain-of-thought moderado |
| `pr_review` | `high` | Análise de qualidade antes de propor mudanças exige avaliação aprofundada |
| `follow_ups` | `low` | Ações pós-merge geralmente são tarefas delimitadas e diretas |

### Cadeia de fallback completa

```
DEILE_PIPELINE_REASONING_<STAGE> / pipeline.reasoning.<stage>   ← nível 1 (per-stage)
         ↓ (se ausente)
DEILE_REASONING_EFFORT / model.reasoning_effort                  ← nível 2 (global)
         ↓ (se ausente)
_STAGE_DEFAULT_REASONING_EFFORT[stage]                           ← nível 3 (default opinado)
         ↓ (fallback de segurança — não atingido em uso normal)
None
```

O operador pode sobrescrever qualquer stage via `settings.json` (chave `pipeline.reasoning.<stage>`) ou env var (`DEILE_PIPELINE_REASONING_<STAGE>`) sem desabilitar os defaults dos demais stages.

Diferença de `reasoning_resolver.resolve_stage_reasoning`: aquela função tem apenas 3 níveis (sem defaults opinados) e é usada no caminho CLI interativo; esta função (`dispatch_resolver.resolve_stage_reasoning_effort`) é usada no pipeline autônomo.

## Forçar um modelo específico

| Aspecto | Detalhe |
|---|---|
| Sintaxe na CLI one-shot | `--model PROVIDER:MODEL_ID` |
| Armazenamento | `session.context_data["forced_model"]` |
| Comportamento em handle ausente | Resposta com `metadata.forced_model_not_registered=True`; CLI renderiza painel específico |
