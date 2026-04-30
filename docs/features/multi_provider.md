# PLANO COMPLETO DE REFATORAÇÃO — DEILE v5.0 → Multi-Provider Model Router

## 1. Diagnóstico do estado atual

### 1.1 Tabela de gap analysis

| RF/RNF | Status atual | Gap | Onde mexer |
|---|---|---|---|
| RF-001 (multi-provider) | Apenas `GeminiProvider` registrado em `deile.py:64`. `model_switcher.py` tem enum `ModelProvider` com `OPENAI/ANTHROPIC` placeholders, mas não está integrado ao runtime. | Criar `AnthropicProvider`, `OpenAIProvider`, `DeepSeekProvider`. | `deile/core/models/`, `deile.py` |
| RF-002 (catálogo de 9 modelos) | Inexistente. Há um catálogo *legado divergente* em `model_switcher.py` (gpt-4, claude-3-opus, etc.) e modelos hardcoded em `model_command.py` (Gemini 2.5). | Criar `ModelCatalog` único, autoritativo, com 9 modelos da especificação. Remover catálogo de `model_switcher.py`. | `deile/core/models/catalog.py` (novo); `model_switcher.py` (deprecar) |
| RF-003 (cascata task-optimized por tier) | `_task_optimized_selection` em `router.py:432` usa `ModelSize.SMALL/MEDIUM/LARGE` e palavras-chave naïve. Não há cascata por provider. | Substituir lógica por cascata `[provider1, provider2, provider3]` por tier. | `deile/core/models/router.py` |
| RF-004 (cost-optimized) | `_cost_optimized_selection` em `router.py:461` usa `cost_per_token * estimated_tokens / success_rate`. Não considera DeepSeek-first. | Reescrever para policy declarativa em YAML. | `router.py` + `model_providers.yaml` |
| RF-005 (detecção de tier) | `IntentAnalyzer` retorna `IntentType` e `IntentCategory` — não retorna tier. `intent_patterns.yaml` não tem padrões de tier. | Adicionar campo `target_tier` em `IntentPattern`, novo método `classify_tier()` no analyzer. | `intent_analyzer.py`, `intent_patterns.yaml` |
| RF-006 (config YAML) | `api_config.yaml` só tem `gemini`. | Criar `model_providers.yaml` completo. | `deile/config/model_providers.yaml` (novo); `manager.py` |
| RF-007 (env vars exclusivamente) | `Settings.get_api_key("gemini")` lê do env. OK. Falta `deepseek` no mapeamento. | Adicionar `DEEPSEEK_API_KEY` em `known_keys` (settings.py:115) e `key_mapping` (settings.py:223). | `deile/config/settings.py` |
| RF-008 (BaseProvider abstrato + 3 impl) | `ModelProvider` ABC existe (`base.py:66`). Não suporta tools no contrato. | Estender ABC com métodos `chat_with_tools`, `generate_stream` unificado, e `provider_id`/`model_id`/`tier`/`pricing`. Manter retrocompat. | `deile/core/models/base.py` |
| RF-009 (function calling cross-provider) | `registry.get_gemini_functions()` retorna `FunctionDeclaration` Gemini. `ToolSchema.to_gemini_function()` em `base.py:72` é Gemini-only. | Adicionar `to_anthropic_tool()`, `to_openai_function()` em `ToolSchema`. Adicionar métodos `get_anthropic_tools()`, `get_openai_functions()` em `ToolRegistry`. | `deile/tools/base.py`, `deile/tools/registry.py` |
| RF-010 (tracking de custo SQLite) | Inexistente. `infrastructure/monitoring/cost_tracker` referenciado em `cost_command.py` provavelmente quebrado/legado. `sqlite_task_manager.py` usa `aiosqlite` em `./deile_tasks.db` — extensão natural. | Criar tabela `model_usage` no mesmo banco. | `deile/orchestration/sqlite_task_manager.py` ou novo módulo `deile/storage/usage_repository.py` |
| RF-011 (prompt caching) | Inexistente. | Implementar por provider: Anthropic `cache_control`, OpenAI/DeepSeek (automático). | Cada `*Provider` |
| RF-012 (circuit breaker por provider) | Existe básico em `router.py` por `error_rate >= 0.8` (`router.py:297`). Por *modelo*, não por *provider*. Não tem `consecutive_failures`. | Reescrever para contador por provider, threshold N (default 3), reset com cooldown. | `router.py` |
| RF-013 (`/model` command) | Hardcoded para Gemini (`model_command.py:52`). Não tem custos sessão / strategy switch. | Reescrever consumindo `ModelCatalog` + `UsageRepository`. | `deile/commands/builtin/model_command.py` |
| RF-014 (streaming unificado) | `ModelProvider.generate_stream()` existe mas devolve `str`. Não tem eventos tipados. | Definir `UnifiedStreamEvent` + reescrever para `AsyncIterator[UnifiedStreamEvent]`. | `base.py` + cada provider |
| RF-015 (limites de orçamento) | Inexistente (referência legada em `cost_tracker` não usada no fluxo principal). | Criar `BudgetGuard` consultando `UsageRepository` antes de cada chamada. | `deile/core/models/budget.py` (novo) |
| RNF-001 (compat Gemini legado) | Atual sistema é Gemini-only — compatibilidade trivial se Gemini virar `optional`. | Refatorar `GeminiProvider` para o novo contrato, ainda registrável. | `gemini_provider.py`, `deile.py` |
| RNF-002 (overhead ≤50ms) | Não medido. `IntentAnalyzer` tem cache LRU (router não tem). | Adicionar benchmark; cachear seleção de tier por hash do input. | Teste novo + `router.py` |
| RNF-003 (observabilidade JSON) | `debug_logger.py` é stub minimalista. `is_debug_enabled` lê `DEILE_DEBUG`. | Estender com eventos estruturados `router.selection`, `router.fallback`, `router.circuit_breaker_open`. | `debug_logger.py` |
| RNF-004 (deps) | `requirements.txt` só tem `google-genai==1.46.0`. | Adicionar `anthropic>=0.40.0`, `openai>=1.50.0`. | `requirements.txt` |
| Regra 1 (lista de modelos com input/output) | Hardcoded textual em `model_command.py` para 3 modelos Gemini. | Tabela rica com 9 modelos lendo `ModelCatalog`. | `model_command.py` |
| Regra 2 (expor JSON de erro) | `chat_with_tools` captura erro da tool, mas erro de provider sobe como `ModelError` genérico. | Capturar `provider.generate()` → criar `ProviderErrorEnvelope` com JSON do erro nativo. | `base.py` (envelope) + cada provider + UI |

### 1.2 Componentes a deprecar

- `deile/core/models/model_switcher.py` (1 arquivo, ~780 linhas): catálogo legado com modelos errados (gpt-4, claude-3-opus, gemini-pro), `ModelProvider` enum duplicado, importa `infrastructure.monitoring.cost_tracker` que pode estar quebrado. **Decisão recomendada**: deprecar o módulo inteiro; reaproveitar conceito de `ModelPerformance`/`SwitchEvent` no novo router se útil.
- `cost_command.py` (referenciando `infrastructure.monitoring.cost_tracker`): manter o nome mas reescrever para usar nova `UsageRepository`. Se `cost_tracker` está quebrado, isso explica deprecação.

---

## 2. Arquitetura alvo

### 2.1 Diagrama ASCII

```
                        ┌────────────────────────────────────────────┐
                        │              DeileAgent.process_input       │
                        └──────────────────────┬─────────────────────┘
                                               │ (user_input, session)
                                               ▼
                        ┌────────────────────────────────────────────┐
                        │   IntentAnalyzer.classify()  [+ tier]       │
                        │   ─ retorna IntentAnalysisResult            │
                        │   ─ NEW: target_tier ∈ {TIER_1..TIER_4}     │
                        └──────────────────────┬─────────────────────┘
                                               │ tier
                                               ▼
                ┌──────────────────────────────────────────────────────────┐
                │                       ModelRouter                        │
                │                                                          │
                │   ┌─ ModelCatalog (YAML) ──────────────────────────┐    │
                │   │ 9 modelos × {provider, model_id, tier,         │    │
                │   │             input_price, output_price,         │    │
                │   │             context_window, capabilities}      │    │
                │   └─────────────────────────────────────────────────┘    │
                │                                                          │
                │   ┌─ RoutingPolicy (task-opt | cost-opt) ──────────┐    │
                │   │ tier → cascade [provider_a, provider_b, _c]     │    │
                │   └─────────────────────────────────────────────────┘    │
                │                                                          │
                │   ┌─ CircuitBreaker (per provider) ────────────────┐    │
                │   │ provider_id → {failures, opened_at, state}      │    │
                │   └─────────────────────────────────────────────────┘    │
                │                                                          │
                │   ┌─ BudgetGuard ──────────────────────────────────┐    │
                │   │ session/daily/monthly USD limits                │    │
                │   │   → bloqueia antes de chamar provider           │    │
                │   └─────────────────────────────────────────────────┘    │
                │                                                          │
                │   select_provider(tier, context) → ProviderHandle        │
                │   execute_with_fallback() → cascata + circuit breaker   │
                └────────────────────┬───────────────────┬─────────────────┘
                                     │                   │
                                     ▼                   ▼
                ┌──────────────────────────────┐  ┌──────────────────────────────┐
                │  UnifiedToolSchema           │  │  UnifiedStreamEvent           │
                │   ToolSchema.to_anthropic()  │  │   text_delta                  │
                │   ToolSchema.to_openai()     │  │   tool_use_start              │
                │   ToolSchema.to_gemini()     │  │   tool_use_delta              │
                └──────────────┬───────────────┘  │   tool_use_end                │
                               │                  │   usage_final                 │
                               ▼                  └──────────────────────────────┘
        ┌──────────────────────────────────────────────────┐
        │                BaseProvider (ABC)                 │
        │ ┌──────┐ ┌──────┐ ┌────────┐ ┌──────────────────┐│
        │ │chat  │ │chat_w│ │stream  │ │estimate_cost     ││
        │ │      │ │_tools│ │        │ │get_pricing       ││
        │ └──────┘ └──────┘ └────────┘ └──────────────────┘│
        └──────┬─────────┬────────────┬────────────┬───────┘
               ▼         ▼            ▼            ▼
        ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────────┐
        │Anthropic │ │OpenAI    │ │DeepSeek  │ │Gemini        │
        │Provider  │ │Provider  │ │Provider  │ │Provider      │
        │          │ │          │ │ (extends │ │ (legacy)     │
        │ tool_use │ │function_ │ │ OpenAI,  │ │ FunctionDecl │
        │ cache_   │ │call      │ │ base_url │ │              │
        │ control  │ │          │ │ swap)    │ │              │
        └────┬─────┘ └────┬─────┘ └────┬─────┘ └──────┬───────┘
             │            │            │              │
             ▼            ▼            ▼              ▼
        ┌────────────────────────────────────────────────────┐
        │     UsageRepository (SQLite — model_usage table)   │
        │  registra: provider, model, tokens, cost, latency  │
        └────────────────────────────────────────────────────┘
                              │
                              ▼
                      ┌─────────────────┐
                      │  /model command │
                      │  /cost  command │
                      └─────────────────┘
```

### 2.2 Tradução de schemas (RF-009)

`UnifiedToolSchema` é o `ToolSchema` existente em `deile/tools/base.py` enriquecido. Cada tool, no `auto_discover`, expõe um único `ToolSchema` com `parameters` em JSON Schema padrão. O registry expõe **3 funções**:
- `get_anthropic_tools()` → `[{"name": ..., "description": ..., "input_schema": {...}}]`
- `get_openai_functions()` → `[{"type": "function", "function": {"name": ..., "description": ..., "parameters": {...}, "strict": false}}]`
- `get_gemini_functions()` (já existe) → `[FunctionDeclaration(...)]`

Mapeamento de tipo: o SDK Anthropic e OpenAI ambos aceitam JSON Schema cru, então `ToolSchema._convert_parameters_to_json_schema` (já existente) é reaproveitado e a tradução vira *passthrough*.

### 2.3 Cascata por tier (RF-003 / RF-004)

Para cada turno:
1. `IntentAnalyzer.classify_tier(user_input)` → `ModelTier`.
2. `RoutingPolicy.resolve(tier, strategy)` → `[ModelHandle_a, ModelHandle_b, ModelHandle_c]` (lista ordenada).
3. Para cada handle (em ordem):
   - Se `CircuitBreaker[handle.provider_id].is_open()` → pula.
   - Se `BudgetGuard.would_exceed(handle, estimated_tokens)` → pula + log.
   - Senão tenta `provider.chat_with_tools(...)`. Em sucesso → retorna. Em erro → registra falha em `CircuitBreaker[handle.provider_id]`, captura JSON do erro, tenta próximo.
4. Se todos falharam → `ModelError("ALL_TIER_PROVIDERS_FAILED")` com lista agregada de JSON errors.

### 2.4 Circuit breaker + tracking de custo

`CircuitBreaker` mantém, por `provider_id`:
- `consecutive_failures: int`
- `state: CLOSED | OPEN | HALF_OPEN`
- `opened_at: float` (timestamp)
- Após `cooldown_seconds` (default 60), passa a `HALF_OPEN`; primeira chamada bem-sucedida → `CLOSED`; falha → `OPEN` de novo.

`UsageRepository`, após cada `chat_with_tools` retornar:
- Calcula `cost_usd = (input_tokens × input_price + output_tokens × output_price) / 1_000_000`.
- `INSERT INTO model_usage (...)`.
- Notifica `BudgetGuard` (atualiza acumulados em memória + invalida cache).

---

## 3. Modelo de dados

### 3.1 Enums e dataclasses novos

```
# deile/core/models/tier.py (NOVO)
class ModelTier(Enum):
    TIER_1 = "tier_1"   # complex coding/refactor/architecture
    TIER_2 = "tier_2"   # default coding/tool use
    TIER_3 = "tier_3"   # fast/classification/simple Q&A
    TIER_4 = "tier_4"   # bulk/batch/cost-critical

# deile/core/models/catalog.py (NOVO)
@dataclass(frozen=True)
class ModelPricing:
    input_per_1m_usd: float
    output_per_1m_usd: float
    cached_input_per_1m_usd: Optional[float] = None  # se SDK reportar

@dataclass(frozen=True)
class ModelHandle:
    provider_id: str       # "anthropic" | "openai" | "deepseek" | "gemini"
    model_id: str          # "claude-opus-4-7" etc.
    tier: ModelTier
    pricing: ModelPricing
    context_window: int
    capabilities: frozenset[str]   # {"function_calling", "vision", "streaming", "caching"}
    display_name: str
    label: str             # "flagship" | "balanced" | "fast" | "ultra-cheap" ...

class ModelCatalog:
    @classmethod
    def from_yaml(cls, path: Path) -> "ModelCatalog": ...
    def get(self, provider_id: str, model_id: str) -> ModelHandle: ...
    def list_by_tier(self, tier: ModelTier) -> List[ModelHandle]: ...
    def list_all(self) -> List[ModelHandle]: ...

# deile/core/models/policy.py (NOVO)
class RoutingStrategyName(Enum):
    TASK_OPTIMIZED = "task_optimized"
    COST_OPTIMIZED = "cost_optimized"

@dataclass
class TierCascade:
    tier: ModelTier
    handles: List[ModelHandle]   # ordem = prioridade

class RoutingPolicy:
    @classmethod
    def from_yaml(cls, path: Path, strategy: RoutingStrategyName) -> "RoutingPolicy": ...
    def resolve(self, tier: ModelTier) -> TierCascade: ...
    def switch_strategy(self, name: RoutingStrategyName) -> None: ...

# deile/core/models/provider_config.py (NOVO)
@dataclass
class ProviderConfig:
    provider_id: str
    api_key_env: str          # "ANTHROPIC_API_KEY"
    base_url: Optional[str]   # None | "https://api.deepseek.com" | ...
    sdk_kwargs: Dict[str, Any]
    enabled: bool = True
    timeout_seconds: int = 120
    max_retries: int = 0      # 0 = router controla; SDK não retry
```

### 3.2 Streaming unificado

```
# deile/core/models/stream_events.py (NOVO)
class StreamEventType(Enum):
    TEXT_DELTA = "text_delta"
    TOOL_USE_START = "tool_use_start"
    TOOL_USE_DELTA = "tool_use_delta"
    TOOL_USE_END = "tool_use_end"
    USAGE_FINAL = "usage_final"
    ERROR = "error"

@dataclass
class UnifiedStreamEvent:
    type: StreamEventType
    text: Optional[str] = None                   # TEXT_DELTA
    tool_call_id: Optional[str] = None           # TOOL_USE_*
    tool_name: Optional[str] = None              # TOOL_USE_START
    arguments_json_delta: Optional[str] = None   # TOOL_USE_DELTA
    arguments: Optional[Dict[str, Any]] = None   # TOOL_USE_END (parsed)
    usage: Optional["ModelUsage"] = None         # USAGE_FINAL
    error_envelope: Optional["ProviderErrorEnvelope"] = None  # ERROR
```

### 3.3 Envelope de erro (Regra 2)

```
# deile/core/models/errors.py (NOVO ou estendido)
@dataclass
class ProviderErrorEnvelope:
    provider_id: str
    model_id: str
    error_type: str                  # "auth", "rate_limit", "invalid_request", "server", "unknown"
    http_status: Optional[int]
    raw_json: Dict[str, Any]         # JSON cru do SDK / response.body parsed
    message: str                     # mensagem humana
    request_id: Optional[str]
    timestamp: float

    def to_display_dict(self) -> Dict[str, Any]: ...

class ProviderInvocationError(ModelError):
    def __init__(self, envelope: ProviderErrorEnvelope, ...): ...
```

### 3.4 Esquema SQLite — tabela `model_usage`

```sql
CREATE TABLE IF NOT EXISTS model_usage (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp     TEXT NOT NULL,          -- ISO 8601
    session_id    TEXT NOT NULL,
    provider_id   TEXT NOT NULL,          -- 'anthropic' | 'openai' | 'deepseek' | 'gemini'
    model_id      TEXT NOT NULL,          -- 'claude-opus-4-7' etc
    tier          TEXT,                   -- 'tier_1' | 'tier_2' | 'tier_3' | 'tier_4'
    input_tokens  INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cached_tokens INTEGER DEFAULT 0,
    cost_usd      REAL NOT NULL,
    latency_ms    INTEGER NOT NULL,
    success       BOOLEAN NOT NULL,
    error_code    TEXT,                   -- 'auth' | 'rate_limit' | etc
    request_id    TEXT,
    metadata      TEXT                    -- JSON blob
);
CREATE INDEX IF NOT EXISTS idx_usage_session ON model_usage(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_provider ON model_usage(provider_id);
CREATE INDEX IF NOT EXISTS idx_usage_timestamp ON model_usage(timestamp);
```

Mantida no mesmo `deile_tasks.db` (path em `Settings.working_directory`) — coabita com `tasks` e `task_lists`.

### 3.5 YAML — `deile/config/model_providers.yaml`

```yaml
# Configuração unificada do Multi-Provider Model Router
# API keys NUNCA são armazenadas aqui — apenas o nome da variável de ambiente.

version: 1
default_strategy: task_optimized   # task_optimized | cost_optimized

providers:
  anthropic:
    api_key_env: ANTHROPIC_API_KEY
    base_url: null                  # SDK default
    enabled: true
    timeout_seconds: 120
    sdk_kwargs:
      default_headers:
        anthropic-beta: prompt-caching-2024-07-31

  openai:
    api_key_env: OPENAI_API_KEY
    base_url: null
    enabled: true
    timeout_seconds: 120
    sdk_kwargs: {}

  deepseek:
    api_key_env: DEEPSEEK_API_KEY
    base_url: https://api.deepseek.com/v1
    enabled: true
    timeout_seconds: 120
    sdk_kwargs: {}

  gemini:                           # legado opcional (RNF-001)
    api_key_env: GOOGLE_API_KEY
    base_url: null
    enabled: false                  # default: desligado
    timeout_seconds: 120
    sdk_kwargs: {}

models:
  # === ANTHROPIC ===
  - provider_id: anthropic
    model_id: claude-opus-4-7
    tier: tier_1
    label: flagship
    display_name: Claude Opus 4.7
    pricing: { input_per_1m_usd: 5.00, output_per_1m_usd: 25.00, cached_input_per_1m_usd: 0.50 }
    context_window: 200000
    capabilities: [function_calling, streaming, caching, vision]

  - provider_id: anthropic
    model_id: claude-sonnet-4-6
    tier: tier_2
    label: balanced
    display_name: Claude Sonnet 4.6
    pricing: { input_per_1m_usd: 3.00, output_per_1m_usd: 15.00, cached_input_per_1m_usd: 0.30 }
    context_window: 200000
    capabilities: [function_calling, streaming, caching, vision]

  - provider_id: anthropic
    model_id: claude-haiku-4-5
    tier: tier_3
    label: fast
    display_name: Claude Haiku 4.5
    pricing: { input_per_1m_usd: 1.00, output_per_1m_usd: 5.00, cached_input_per_1m_usd: 0.10 }
    context_window: 200000
    capabilities: [function_calling, streaming, caching]

  # === OPENAI ===
  - provider_id: openai
    model_id: gpt-5.3-codex
    tier: tier_1
    label: code-specialist
    display_name: GPT-5.3 Codex
    pricing: { input_per_1m_usd: 1.75, output_per_1m_usd: 14.00 }
    context_window: 200000
    capabilities: [function_calling, streaming, caching]

  - provider_id: openai
    model_id: gpt-5.4
    tier: tier_2
    label: balanced
    display_name: GPT-5.4
    pricing: { input_per_1m_usd: 2.50, output_per_1m_usd: 15.00 }
    context_window: 200000
    capabilities: [function_calling, streaming, caching, vision]

  - provider_id: openai
    model_id: gpt-5.4-mini
    tier: tier_3
    label: fast
    display_name: GPT-5.4 Mini
    pricing: { input_per_1m_usd: 0.75, output_per_1m_usd: 4.50 }
    context_window: 128000
    capabilities: [function_calling, streaming, caching]

  # === DEEPSEEK ===
  - provider_id: deepseek
    model_id: deepseek-v4-pro
    tier: tier_1
    label: flagship-cheap
    display_name: DeepSeek V4 Pro
    pricing: { input_per_1m_usd: 1.74, output_per_1m_usd: 3.48 }
    context_window: 128000
    capabilities: [function_calling, streaming, caching]

  - provider_id: deepseek
    model_id: deepseek-v4-flash
    tier: tier_3
    label: ultra-cheap
    display_name: DeepSeek V4 Flash
    pricing: { input_per_1m_usd: 0.14, output_per_1m_usd: 0.28 }
    context_window: 128000
    capabilities: [function_calling, streaming, caching]

  - provider_id: deepseek
    model_id: deepseek-reasoner
    tier: tier_3
    label: reasoning-cheap
    display_name: DeepSeek Reasoner
    pricing: { input_per_1m_usd: 0.14, output_per_1m_usd: 0.28 }
    context_window: 128000
    capabilities: [streaming, caching]

policies:
  task_optimized:
    tier_1: [anthropic:claude-opus-4-7, openai:gpt-5.3-codex, deepseek:deepseek-v4-pro]
    tier_2: [anthropic:claude-sonnet-4-6, openai:gpt-5.4, deepseek:deepseek-v4-pro]
    tier_3: [anthropic:claude-haiku-4-5, openai:gpt-5.4-mini, deepseek:deepseek-v4-flash]
    tier_4: [deepseek:deepseek-v4-flash, openai:gpt-5.4-mini]

  cost_optimized:
    tier_1: [deepseek:deepseek-v4-pro, openai:gpt-5.4]   # Anthropic só sob pedido manual explícito
    tier_2: [deepseek:deepseek-v4-pro, openai:gpt-5.4-mini]
    tier_3: [deepseek:deepseek-v4-flash, openai:gpt-5.4-mini]
    tier_4: [deepseek:deepseek-v4-flash, openai:gpt-5.4-mini]

circuit_breaker:
  consecutive_failures_threshold: 3
  cooldown_seconds: 60
  half_open_test_requests: 1

budget:
  enabled: true
  per_session_usd: 5.00       # default; null = sem limite
  per_provider_daily_usd:
    anthropic: 50.00
    openai: 50.00
    deepseek: 10.00
  per_provider_monthly_usd:
    anthropic: 500.00
    openai: 500.00
    deepseek: 100.00
  alert_threshold_pct: 80     # warn em 80% do limite
```

---

## 4. Plano de fases

> **Princípio**: cada fase termina com `pytest deile/tests/` verde e `python deile.py` rodando ao menos com Gemini. Nenhuma fase quebra a anterior.

---

### **Fase 0 — Preparação**

**Objetivo**: criar infraestrutura mínima sem alterar comportamento.

**Arquivos a criar**
- `deile/config/model_providers.yaml` — YAML completo da §3.5.
- `deile/core/models/tier.py` — apenas `ModelTier` enum.
- `deile/tests/test_model_catalog_loading.py` — testa parse do YAML.

**Arquivos a modificar**
- `requirements.txt`: adicionar `anthropic>=0.40.0`, `openai>=1.50.0` (DeepSeek reaproveita `openai`). **Nota**: verificar via context7 a versão *atual estável* do SDK Anthropic e OpenAI antes de fixar pin — o requisito diz `>=0.40.0` e `>=1.50.0`, manter assim.
- `deile/config/settings.py`: linha 115 adicionar `"DEEPSEEK_API_KEY"` em `known_keys`; linha 223 adicionar `"deepseek": "DEEPSEEK_API_KEY"` em `key_mapping`.

**Arquivos a remover/deprecar**: nenhum.

**Testes a criar**
- `deile/tests/test_settings_api_keys.py`: confirma que `Settings().get_api_key("deepseek")` lê `DEEPSEEK_API_KEY`.
- `deile/tests/test_model_providers_yaml.py`: carrega o YAML, valida 9 modelos, 4 tiers, 2 policies.

**Critérios de aceitação**
- `pip install -r requirements.txt` instala `anthropic` e `openai` sem conflito.
- `pytest deile/tests/test_settings_api_keys.py deile/tests/test_model_providers_yaml.py` passa.
- `python deile.py` continua bootando com Gemini.

**Riscos / mitigações**
- *Conflito de versão `httpx`*: SDKs Anthropic e OpenAI podem requerer versões diferentes. **Mitigação**: rodar `pip-compile` (ou `pip check`) e fixar `httpx>=0.27`.
- *YAML mal formado*: validar via `pytest` antes da Fase 1.

---

### **Fase 1 — Refator do `BaseProvider`**

**Objetivo**: contrato unificado expressando tools, streaming, custo e caching, sem quebrar `GeminiProvider` atual.

**Arquivos a criar**
- `deile/core/models/stream_events.py` (§3.2).
- `deile/core/models/errors.py` (§3.3 — `ProviderErrorEnvelope`, `ProviderInvocationError`).
- `deile/core/models/provider_config.py` (`ProviderConfig`).
- `deile/core/models/catalog.py` (`ModelHandle`, `ModelPricing`, `ModelCatalog`).

**Arquivos a modificar**
- `deile/core/models/base.py`:
  - **Adicionar** propriedade abstrata `provider_id: str` (separa do `provider_name` legado, retorna mesma string).
  - **Adicionar** propriedade abstrata `tier: ModelTier`.
  - **Adicionar** propriedade abstrata `pricing: ModelPricing`.
  - **Adicionar** método abstrato `async def chat_with_tools(messages, tools, system_instruction=None, **kwargs) -> Tuple[str, List[ToolResult], ModelUsage]`.
  - **Alterar** assinatura de `generate_stream()` para `AsyncIterator[UnifiedStreamEvent]` (manter wrapper de retrocompat se necessário).
  - **Reescrever** `estimate_cost(usage)` para usar `self.pricing` (input + output + cached).
  - **Adicionar** método concreto `_record_usage(session_id, usage, latency_ms, success, error_envelope)` que delega ao `UsageRepository` (Fase 11).
  - **Manter** `ModelType`, `ModelMessage`, `ModelResponse`, `ModelUsage` intactos para retrocompat.
  - **Manter** `ModelSize` enum por compatibilidade — adicionar mapping `ModelTier ↔ ModelSize` (TIER_1→LARGE, TIER_2→MEDIUM, TIER_3/TIER_4→SMALL) numa função utilitária.

**Arquivos a remover/deprecar**: nenhum nesta fase.

**Testes a criar**
- `deile/tests/test_base_provider_contract.py`: instancia uma subclasse mock (in-memory) e valida que todas as 4 propriedades + `chat_with_tools` + `generate_stream` funcionam.
- `deile/tests/test_provider_error_envelope.py`: serializa `ProviderErrorEnvelope` para dict.

**Critérios de aceitação**
- Mock provider implementa novo contrato e passa nos testes.
- `GeminiProvider` continua bootando sem erro (mesmo ainda no contrato antigo — implementaremos os novos métodos como wrappers nas próximas fases).

**Riscos**
- Quebrar retrocompat de `ModelProvider`. **Mitigação**: novos métodos são abstratos *opcionais via default `NotImplementedError`* até Fase 6.

---

### **Fase 2 — Tradutor unificado de schemas de tools**

**Objetivo**: tools definidas uma vez expostas em 3 formatos.

**Arquivos a criar**
- `deile/tools/schema_translators.py`:
  - `def to_anthropic_tool(schema: ToolSchema) -> Dict`
  - `def to_openai_function(schema: ToolSchema) -> Dict`
  - `def to_gemini_function(schema: ToolSchema)` — delega para método existente.

**Arquivos a modificar**
- `deile/tools/base.py`:
  - Em `ToolSchema`, adicionar:
    - `def to_anthropic_tool(self) -> Dict[str, Any]`: retorna `{"name", "description", "input_schema": params_json_schema}`.
    - `def to_openai_function(self) -> Dict[str, Any]`: retorna `{"type": "function", "function": {"name", "description", "parameters": params_json_schema}}`.
- `deile/tools/registry.py`:
  - Adicionar `def get_anthropic_tools(authorized_only=True, security_level=None) -> List[Dict]`.
  - Adicionar `def get_openai_functions(authorized_only=True, security_level=None) -> List[Dict]`.
  - Não tocar em `get_gemini_functions()` (já existe).

**Arquivos a remover**: nenhum.

**Testes a criar**
- `deile/tests/test_tool_schema_translation.py`:
  - Para 3 tools representativas (`bash`, `read_file`, `find`), valida output Anthropic, OpenAI, Gemini.
  - Property test: roundtrip parameters JSON Schema é idêntico em todos os formatos.

**Critérios de aceitação**
- `len(registry.get_anthropic_tools()) == len(registry.get_gemini_functions()) == len(registry.get_openai_functions())`.
- `pytest deile/tests/test_tool_schema_translation.py` verde.

**Riscos**
- *Tipos JSON Schema com casing inconsistente*: `_convert_parameters_to_json_schema` já normaliza `STRING→string`. Validar em testes.
- *OpenAI strict mode*: nem todas as tools são compatíveis com `strict: true`. **Mitigação**: emitir `strict: false` por padrão.

---

### **Fase 3 — `AnthropicProvider`**

**Objetivo**: implementação real com tool_use + cache_control + streaming.

**Arquivos a criar**
- `deile/core/models/anthropic_provider.py`:
  - Classe `AnthropicProvider(ModelProvider)`.
  - `__init__(model_handle: ModelHandle, provider_config: ProviderConfig, **kwargs)`.
  - `provider_id = "anthropic"`, `tier`/`pricing`/`model_name` lidos do `model_handle`.
  - Cliente: `from anthropic import AsyncAnthropic; self.client = AsyncAnthropic(api_key=os.getenv(provider_config.api_key_env), **provider_config.sdk_kwargs)`.
  - **`generate(messages, system_instruction, **kwargs)`**: converte `ModelMessage` → Anthropic message format (`[{"role": "user|assistant", "content": "..."}]`), chama `client.messages.create(...)`. Captura `anthropic.APIError` → cria `ProviderErrorEnvelope` com `e.status_code`, `e.body` (já é dict JSON), `e.request_id`.
  - **`chat_with_tools(messages, tools, system_instruction, **kwargs)`**: loop manual semelhante ao `GeminiProvider.chat_with_tools` mas usando `tool_use` blocks:
    1. Converte `tools` (lista de `ToolSchema`) → `[t.to_anthropic_tool() for t in tools]`.
    2. `response = await client.messages.create(model=..., system=system_instruction, messages=..., tools=anthropic_tools, max_tokens=..., extra_headers={"anthropic-beta": "prompt-caching-2024-07-31"})`.
    3. Itera `response.content` blocks: `text` → acumula; `tool_use` → resolve via `ToolRegistry.execute_function_call`.
    4. Append `{"role": "assistant", "content": response.content}` e `{"role": "user", "content": [{"type": "tool_result", "tool_use_id": ..., "content": ...}]}` em messages.
    5. Repete até `stop_reason != "tool_use"` ou `max_iterations` atingido.
  - **`generate_stream`**: implementar via `client.messages.stream(...)` e mapear eventos (`text` → `TEXT_DELTA`, `input_json_delta` → `TOOL_USE_DELTA`, etc.).
  - **Prompt caching**: suportado via `cache_control: {"type": "ephemeral"}` adicionado ao último bloco do `system` e/ou ao último `user` message (RF-011).

**Arquivos a modificar**: nenhum (este provider é greenfield).

**Testes a criar**
- `deile/tests/test_anthropic_provider.py` (com mock de `AsyncAnthropic`):
  - `generate` simples → ModelResponse válido.
  - `chat_with_tools` com 1 tool call.
  - `chat_with_tools` com 2 iterações sequenciais.
  - Erro 401 → `ProviderInvocationError` com `envelope.error_type == "auth"` e `envelope.raw_json` populado.
  - Streaming yield 3 `TEXT_DELTA` + 1 `USAGE_FINAL`.

**Critérios de aceitação**
- Smoke test integrado (manual): `ANTHROPIC_API_KEY=... python -c "import asyncio; from deile.core.models.anthropic_provider import AnthropicProvider; ..."` retorna texto.
- `pytest deile/tests/test_anthropic_provider.py` verde.

**Riscos**
- *Versão do SDK*: API do `anthropic` muda entre 0.x. **Verificar via context7 antes de implementar** (`mcp__context7__query-docs` em "anthropic-sdk-python tool_use"). Possível necessidade de `client.beta.messages.create` em vez de `client.messages.create` para caching.
- *Loop manual de tools*: Anthropic devolve `stop_reason: "tool_use"` e `content` com blocos `tool_use`; precisa-se enviar `tool_result` como bloco no próximo turno. Comportamento diferente de Gemini.

---

### **Fase 4 — `OpenAIProvider`**

**Objetivo**: implementação real com `function_call` + automatic prefix caching + streaming.

**Arquivos a criar**
- `deile/core/models/openai_provider.py`:
  - Classe `OpenAIProvider(ModelProvider)`.
  - Cliente: `from openai import AsyncOpenAI; self.client = AsyncOpenAI(api_key=..., base_url=provider_config.base_url, **provider_config.sdk_kwargs)`.
  - **`generate`**: `client.chat.completions.create(model=..., messages=[{"role": ..., "content": ...}], ...)`.
  - **`chat_with_tools`**: loop manual:
    1. Converte tools → `[t.to_openai_function() for t in tools]`.
    2. `response = await client.chat.completions.create(model=..., messages=..., tools=openai_tools, tool_choice="auto", ...)`.
    3. Se `response.choices[0].message.tool_calls`: para cada `tc` em tool_calls, executa via `ToolRegistry.execute_function_call(tc.function.name, json.loads(tc.function.arguments))`.
    4. Append `{"role": "assistant", "content": ..., "tool_calls": [...]}` + `[{"role": "tool", "tool_call_id": tc.id, "content": json.dumps(payload)}]`.
    5. Repete até `finish_reason != "tool_calls"`.
  - **`generate_stream`**: `client.chat.completions.create(stream=True, ...)`; mapeia `delta.content` → `TEXT_DELTA`, `delta.tool_calls[*]` → `TOOL_USE_*`.
  - **Caching**: OpenAI faz prefix caching automático para prompts ≥1024 tokens — apenas registrar `cached_tokens` do `usage.prompt_tokens_details.cached_tokens` quando disponível.

**Arquivos a modificar**: nenhum.

**Testes a criar**
- `deile/tests/test_openai_provider.py` (mock `AsyncOpenAI`): equivalentes aos Anthropic.
- Validação de `cached_tokens` parsing.

**Critérios de aceitação**: idem Fase 3.

**Riscos**
- *`tool_calls` chegam em delta separadamente*: streaming OpenAI envia `function.name` num delta e `function.arguments` em vários — precisa concatenação. Verificar via context7 SDK `openai-python>=1.50` antes de implementar.

---

### **Fase 5 — `DeepSeekProvider`**

**Objetivo**: subclasse de `OpenAIProvider` com `base_url` apontando para DeepSeek.

**Arquivos a criar**
- `deile/core/models/deepseek_provider.py`:
  - `class DeepSeekProvider(OpenAIProvider)`.
  - `provider_id = "deepseek"`.
  - `__init__`: chama `super().__init__` com `provider_config` que já tem `base_url=https://api.deepseek.com/v1` e `api_key_env=DEEPSEEK_API_KEY`.
  - Override mínimo: ajustar `model_id` mapping se DeepSeek usa nomes diferentes em `chat.completions.create` (ex.: `deepseek-chat` vs nosso `deepseek-v4-pro`). **Verificar via context7 docs DeepSeek**.

**Arquivos a modificar**: nenhum.

**Testes a criar**
- `deile/tests/test_deepseek_provider.py`: confirma `provider_id == "deepseek"`, confirma que cliente usa `base_url` correto, faz mock de tool call.

**Critérios de aceitação**
- Mesmas features de `OpenAIProvider` rodam com `base_url` da DeepSeek.

**Riscos**
- *Function calling DeepSeek é OpenAI-compatível mas o `tool_choice` pode ter limitações*. **Verificar via context7**.
- *DeepSeek Reasoner* (`deepseek-reasoner`) não suporta tools — `capabilities` não inclui `function_calling`. Router deve respeitar isso ao filtrar candidatos para um turno que requer tools.

---

### **Fase 6 — Refator do `GeminiProvider` para o novo contrato**

**Objetivo**: tornar Gemini um cidadão de primeira classe do novo contrato (não mais o caminho preferencial).

**Arquivos a modificar**
- `deile/core/models/gemini_provider.py`:
  - Adicionar `provider_id = "gemini"`, `tier`/`pricing` lidos de `ModelHandle`.
  - **Renomear** `model_size` para retornar `ModelSize` traduzido a partir de `tier` (manter retrocompat).
  - **Manter** `chat_with_tools` (já existe) — apenas ajustar assinatura para receber `tools: List[ToolSchema]` em vez de buscar do registry internamente. Adapter no agent traduz.
  - Reescrever `generate_stream` para retornar `AsyncIterator[UnifiedStreamEvent]` em vez de `str`.
  - Capturar `genai.errors.APIError` → `ProviderErrorEnvelope` (status_code, response.text parsed como JSON quando possível).

**Arquivos a remover**: nenhum, mas marcar `GeminiProvider` como `legacy=True` no docstring (RNF-001).

**Testes a criar**
- `deile/tests/test_gemini_provider_unified_contract.py`: que `provider_id`/`tier`/`pricing`/streaming events estejam corretos.
- Manter `deile/tests/test_gemini_function_calling.py` existente passando.

**Critérios de aceitação**
- Sessão Gemini legado continua funcionando (smoke test manual).
- Streaming retorna `UnifiedStreamEvent`s.

**Riscos**
- Quebra de `_generate_response_stream` em `agent.py:1016`. **Mitigação**: agent passa a consumir `UnifiedStreamEvent` (Fase 12).

---

### **Fase 7 — `ModelCatalog` + `ModelTier` + tier-aware router**

**Objetivo**: substituir `ModelSize` no router por `ModelTier`.

**Arquivos a criar**
- `deile/core/models/policy.py` (§3 modelo `RoutingPolicy`).
- `deile/core/models/circuit_breaker.py`:
  - `class ProviderCircuitBreaker`: API `record_success(provider_id)`, `record_failure(provider_id)`, `is_open(provider_id) -> bool`, `try_half_open(provider_id)`.

**Arquivos a modificar**
- `deile/core/models/router.py`:
  - **Substituir** `task_model_mapping: Dict[str, ModelSize]` por `policy: RoutingPolicy` injetado.
  - **Substituir** `_task_optimized_selection` por novo `_resolve_cascade(tier) -> List[ModelHandle]` consumindo `policy.resolve(tier)`.
  - **Substituir** `_circuit_breaker_status` (por `provider_key`) por `ProviderCircuitBreaker` (por `provider_id`).
  - **Reescrever** `execute_with_fallback` para iterar `cascade` em ordem, pulando providers com circuit aberto, e capturando `ProviderInvocationError` por handle.
  - **Manter** `RoutingStrategy` enum mas restringir aos dois usados (TASK_OPTIMIZED, COST_OPTIMIZED) + WARN para os outros.
  - **Adicionar** `set_strategy(name: RoutingStrategyName)` que delega a `policy.switch_strategy()`.
  - Adicionar `register_provider(handle: ModelHandle, factory: Callable[[], ModelProvider])` em vez de instância eager — instancia sob demanda (lazy).

**Arquivos a remover**: nenhum nesta fase.

**Testes a criar**
- `deile/tests/test_router_cascade.py`: 
  - registra 3 providers mock (anthropic, openai, deepseek).
  - configura `tier=TIER_1` task_optimized → seleção retorna anthropic.
  - força falha em anthropic → fallback para openai.
  - circuit breaker abre após 3 falhas em anthropic → próxima request seleciona openai diretamente.
- `deile/tests/test_circuit_breaker.py`: state machine CLOSED→OPEN→HALF_OPEN→CLOSED.

**Critérios de aceitação**
- Cascata funciona em testes integrados com mocks.
- `pytest deile/tests/` continua verde.

**Riscos**
- Lazy provider instantiation pode mascarar erros de config até a primeira chamada. **Mitigação**: `register_provider` faz validação leve (api_key presente) imediatamente.

---

### **Fase 8 — `IntentAnalyzer` classificação de tier**

**Objetivo**: estender `IntentAnalyzer` para retornar `ModelTier` adicional.

**Arquivos a modificar**
- `deile/core/intent_analyzer.py`:
  - Em `IntentPattern` adicionar `target_tier: Optional[ModelTier] = None`.
  - Em `IntentAnalysisResult` adicionar `target_tier: Optional[ModelTier] = None`.
  - Em `_combine_analysis_results`, popular `target_tier` baseado em:
    - Se algum `detected_pattern` tem `target_tier` explícito → usar o de maior `confidence_weight`.
    - Senão, fallback heurístico:
      - `complexity_score > 0.7` ou category=IMPLEMENTATION+complexity>0.5 → `TIER_1`.
      - `IntentType.MULTI_STEP` ou category=MODIFICATION → `TIER_2`.
      - `IntentType.SIMPLE_TASK` ou category=INFORMATION → `TIER_3`.
      - input curto (< 50 chars) com keyword "classify" / "y/n" → `TIER_4`.
  - Adicionar `def classify_tier(user_input: str, parse_result, session_context) -> ModelTier` como atalho (chama `analyze` e retorna `result.target_tier or TIER_2`).

**Arquivos a remover**: nenhum.

**Testes a criar**
- `deile/tests/test_intent_tier_classification.py`:
  - "Refactor entire authentication module" → TIER_1.
  - "Read this file and explain it" → TIER_2/TIER_3.
  - "What's 2+2?" → TIER_3.
  - "Classify these 100 records as spam/ham" → TIER_4.

**Critérios de aceitação**
- Acurácia ≥80% num conjunto de 20 inputs rotulados (definidos no teste).
- Cache LRU continua funcionando.

**Riscos**
- *Drift de heurística*: thresholds podem precisar tuning. **Mitigação**: configuráveis via `intent_patterns.yaml settings`.

---

### **Fase 9 — Padrões de tier em `intent_patterns.yaml`**

**Objetivo**: padrões explícitos por tier para classificação de alta confiança.

**Arquivos a modificar**
- `deile/config/intent_patterns.yaml`:
  - Adicionar bloco `# === TIER PATTERNS ===` após linha 117 (`analysis_simple`).
  - Para cada `intent_pattern` existente, adicionar campo `target_tier` (ex: `implementation_complex` → `tier_1`, `implementation_simple` → `tier_3`, `analysis_comprehensive` → `tier_1`, `analysis_simple` → `tier_3`).
  - Adicionar novos padrões dedicados:
    - `tier_1_architecture`: keywords `arquitetura`, `architecture`, `redesign`, `refactor everything`, `system-wide`. `target_tier: tier_1`.
    - `tier_4_bulk`: keywords `bulk`, `batch`, `lote`, `processar todos`, `classify these`, `tag each`. `target_tier: tier_4`.

**Testes a criar**
- `deile/tests/test_intent_yaml_tier_loading.py`: confirma que cada pattern tem `target_tier` válido.

**Critérios de aceitação**
- YAML carrega sem warnings.
- Acurácia da Fase 8 sobe (idealmente ≥90%).

---

### **Fase 10 — Estratégias `task_optimized` / `cost_optimized`**

**Objetivo**: seleção concreta usando `RoutingPolicy`.

**Arquivos a modificar**
- `deile/core/models/router.py`: já preparado na Fase 7. Aqui apenas:
  - Garantir que `select_provider(tier, strategy_override=None)` aceita override por chamada (útil para `/model strategy ...`).
  - `default_strategy` lido de `model_providers.yaml` na inicialização.

**Arquivos a remover**: nenhum.

**Testes a criar**
- `deile/tests/test_routing_policies.py`:
  - task_optimized + tier_1 → cascade `[anthropic:opus, openai:codex, deepseek:pro]`.
  - cost_optimized + tier_1 → cascade `[deepseek:pro, openai:5.4]` (sem anthropic).
  - switch dinâmico durante runtime preserva métricas existentes.

**Critérios de aceitação**
- Switch de strategy via API muda comportamento sem restart.

---

### **Fase 11 — Tracking de custo (SQLite)**

**Objetivo**: persistir cada chamada e expor agregações.

**Arquivos a criar**
- `deile/storage/usage_repository.py`:
  - `class UsageRepository`:
    - `__init__(db_path)`: usa `aiosqlite`. `_initialize_database()` cria tabela `model_usage` (§3.4).
    - `async def record(self, usage_record: UsageRecord) -> None`.
    - `async def get_session_total_usd(session_id) -> float`.
    - `async def get_provider_total_usd(provider_id, since: datetime) -> float`.
    - `async def get_aggregates(group_by="provider", since=None) -> List[Dict]`.
  - `def get_usage_repository() -> UsageRepository` (singleton).
- `deile/core/models/budget.py`:
  - `class BudgetGuard`:
    - `__init__(repository, config: BudgetConfig)`.
    - `async def check(session_id, provider_id, estimated_cost_usd) -> BudgetCheckResult`. Resultado pode ser `OK`, `WARN_THRESHOLD`, `BLOCKED`.

**Arquivos a modificar**
- `deile/core/models/router.py`: antes de cada chamada de provider, `BudgetGuard.check(...)`. Se `BLOCKED`, levanta `BudgetExceededError` (subclasse de `ModelError`).
- Cada `*Provider`: após chamada, `await usage_repository.record(...)` com `provider_id`, `model_id`, `tier`, `input_tokens`, `output_tokens`, `cached_tokens`, `cost_usd`, `latency_ms`, `success`, `error_code`, `request_id`.

**Arquivos a remover**: nenhum.

**Testes a criar**
- `deile/tests/test_usage_repository.py`: insere 5 records, valida agregação por session + por provider.
- `deile/tests/test_budget_guard.py`: 
  - sessão atinge 80% → WARN.
  - sessão atinge 100% → BLOCKED.
  - reset diário (mock data).

**Critérios de aceitação**
- Tabela criada em `deile_tasks.db` no primeiro uso.
- `/cost` (Fase 15 indireto) consulta corretamente.

**Riscos**
- *Concurrência sqlite*: já usa `_db_lock` no padrão do `sqlite_task_manager`. **Mitigação**: reaproveitar pattern.

---

### **Fase 12 — Streaming unificado**

**Objetivo**: agent consome `UnifiedStreamEvent` em vez de `str`.

**Arquivos a modificar**
- `deile/core/agent.py` linha 1016 (`_generate_response_stream`): adapta para iterar `UnifiedStreamEvent` e re-emite para a UI.
- `deile/ui/...` (verificar `display_response`): adicionar suporte a renderizar streaming token-a-token e tool_use_start/end com banners.

**Arquivos a remover**: nenhum.

**Testes a criar**
- `deile/tests/test_streaming_unified.py`: instancia 3 mock providers (Anthropic/OpenAI/Gemini) que produzem mesma sequência de eventos; agent agrega corretamente.

**Critérios de aceitação**
- `python deile.py` mostra texto streaming token-a-token em todos os providers.

**Riscos**
- *Encoding edge cases* nos deltas (UTF-8 multi-byte split). **Mitigação**: SDKs já lidam.

---

### **Fase 13 — Prompt caching cross-provider**

**Objetivo**: ativar caching nativo onde possível.

**Arquivos a modificar**
- `deile/core/models/anthropic_provider.py`:
  - System prompt + tools schema → últimos blocos com `cache_control: {"type": "ephemeral"}`.
  - Reportar `usage.cache_creation_input_tokens` e `usage.cache_read_input_tokens` em `ModelUsage.cached_tokens`.
- `deile/core/models/openai_provider.py`:
  - Já é automático para prompts ≥1024 tokens. Apenas extrair `usage.prompt_tokens_details.cached_tokens`.
- `deile/core/models/deepseek_provider.py`:
  - DeepSeek expõe `usage.prompt_cache_hit_tokens` / `usage.prompt_cache_miss_tokens` — mapear.
- `deile/core/models/gemini_provider.py`: opcional — Gemini 1.5 tem context caching (precisa upload prévio); fora do escopo desta fase.

**Testes a criar**
- `deile/tests/test_prompt_caching_anthropic.py`: mock retorna `cache_creation_input_tokens=1000` → record record com `cached_tokens=1000`.
- Idem OpenAI / DeepSeek.

**Critérios de aceitação**
- `model_usage.cached_tokens > 0` para chamadas com cache hit observado em smoke test manual.

**Riscos**
- *Cache control headers*: Anthropic exige beta header. Já incluído em `sdk_kwargs.default_headers`.

---

### **Fase 14 — Circuit breaker por provider + JSON de erro**

**Objetivo**: cumprir RF-012 + Regra 2 (expor JSON do erro).

**Arquivos a modificar**
- `deile/core/models/circuit_breaker.py`: já criado na Fase 7. Polish:
  - Threshold lido de `model_providers.yaml.circuit_breaker.consecutive_failures_threshold`.
  - Cooldown lido de `cooldown_seconds`.
- `deile/core/models/router.py`:
  - Em `execute_with_fallback`, na captura de `ProviderInvocationError`, agregar `error_envelope` em `errors_by_handle: Dict[str, ProviderErrorEnvelope]`.
  - Se cascata inteira falhar, levantar `ModelError("ALL_TIER_PROVIDERS_FAILED")` com `errors_by_handle` no `metadata`.
- `deile/core/agent.py`:
  - Capturar `ModelError` em `process_input` e passar `errors_by_handle` para a UI.
- `deile/ui/...`:
  - Quando há `errors_by_handle`, renderizar painel com cada provider + JSON cru (`json.dumps(envelope.raw_json, indent=2)`).
- `deile/storage/debug_logger.py`:
  - Estender com método `async def log_router_event(event_type, payload)`. Eventos: `provider_selected`, `cascade_fallback`, `circuit_breaker_opened`, `circuit_breaker_closed`, `budget_exceeded`. Output JSON em `logs/router_events.jsonl`.

**Testes a criar**
- `deile/tests/test_router_error_exposure.py`:
  - Forçar todos os 3 providers do tier_1 a retornar 401 (mock).
  - Validar que `ModelError.metadata["errors_by_handle"]` contém 3 envelopes com `raw_json` e `http_status=401`.
- `deile/tests/test_router_observability.py`: valida que cada seleção emite evento JSON.

**Critérios de aceitação**
- Quando todos os providers falham, console mostra painel detalhado com JSON de cada erro.
- `tail -f logs/router_events.jsonl` durante uma sessão mostra eventos estruturados.

---

### **Fase 15 — Refator do `/model` command**

**Objetivo**: cumprir RF-013 + Regra 1 (input/output cost na lista).

**Arquivos a modificar**
- `deile/commands/builtin/model_command.py`:
  - **Remover** o dict `self.available_models` hardcoded.
  - **Injetar** `model_catalog: ModelCatalog`, `model_router: ModelRouter`, `usage_repo: UsageRepository`.
  - Subcomandos:
    - `/model` (sem args) → equivalente a `/model list`.
    - `/model list` → tabela Rich com colunas `Provider | Model ID | Tier | Input $/1M | Output $/1M | Context | Capabilities | ✓ ative`. Ler de `model_catalog.list_all()`.
    - `/model current` → exibe modelo ativo + tier + cascata atual.
    - `/model use <provider>:<model_id>` → força modelo específico para próxima request (substitui RoutingPolicy.resolve por handle único). Persiste em `session.context_data["forced_model"]`.
    - `/model use auto` → volta para resolução automática.
    - `/model strategy task_optimized | cost_optimized` → `model_router.set_strategy(...)`.
    - `/model cost` → tabela com `usage_repo.get_aggregates(group_by="provider", since=session.created_at)` + total da sessão. Inclui contagem de chamadas, tokens in/out, $ acumulado.
    - `/model budget` → mostra limites e consumido.

**Testes a criar**
- `deile/tests/test_model_command.py`:
  - `/model list` retorna tabela com 9 modelos e mostra input/output cost.
  - `/model use anthropic:claude-haiku-4-5` força modelo.
  - `/model strategy cost_optimized` muda comportamento subsequente.
  - `/model cost` retorna agregação correta.

**Critérios de aceitação**
- Tabela Rich renderiza corretamente com 9 modelos.
- Switch persiste para a sessão atual.

**Riscos**
- *StaticCommandRegistry*: arquivo registra com `StaticCommandRegistry.register("model", ModelCommand)` (linha 245). Verificar que continua compatível.

---

### **Fase 16 — `deile.py` entry point**

**Objetivo**: registro condicional sem bloqueio em ausência de Gemini.

**Arquivos a modificar**
- `deile.py`:
  - **Remover** o bloco linhas 56-61 que `return False` se `gemini` API key ausente.
  - **Substituir** por (em prosa):
    1. Carregar `model_providers.yaml` via `ConfigManager`.
    2. Para cada provider em `providers` no YAML com `enabled: true`:
       - Verificar se `os.getenv(provider.api_key_env)` está presente.
       - Se ausente: logar warning amarelo `"⚠ Provider {id} desabilitado: {api_key_env} não definida"` e pular.
       - Se presente: instanciar todos os `ModelHandle`s do catálogo daquele provider e registrar no router via `register_provider(handle, factory_lambda)`.
    3. Validar que ao menos 1 provider ativo. Se zero: `display_error("Nenhum provider configurado. Defina ao menos uma de: ANTHROPIC_API_KEY, OPENAI_API_KEY, DEEPSEEK_API_KEY, GOOGLE_API_KEY.")` e exit.

**Testes a criar**
- `deile/tests/test_bootstrap.py`:
  - Sem nenhuma env var → `initialize()` retorna False com mensagem útil.
  - Com `ANTHROPIC_API_KEY` apenas → boot ok, router tem 3 modelos Anthropic.
  - Com todas as 3 → 9 modelos disponíveis.

**Critérios de aceitação**
- `unset GOOGLE_API_KEY; ANTHROPIC_API_KEY=sk-... python deile.py` boota sem reclamar de Gemini.

---

### **Fase 17 — Testes de integração + benchmark de overhead**

**Objetivo**: cobertura ≥80% + RNF-002.

**Testes a criar**
- `deile/tests/integration/test_e2e_anthropic.py`: roda real (com env var), pede `2+2`, valida resposta. Skip se `ANTHROPIC_API_KEY` ausente.
- `deile/tests/integration/test_e2e_tool_calling.py`: pede para listar arquivos via `/model use anthropic:...` + tool `bash`, valida que `ToolResult` retorna saída do `ls`.
- `deile/tests/integration/test_e2e_fallback.py`: usa env var falsa para Anthropic, válida para OpenAI; valida que cascata cai para OpenAI e completa.
- `deile/tests/perf/test_router_overhead.py`: 1000 chamadas a `select_provider(tier=TIER_2)` com mock providers; assert `(end - start) / 1000 < 0.05`. Mede tradução de schema separadamente.

**Critérios de aceitação**
- Overhead médio do router < 50ms (RNF-002).
- `pytest --cov=deile` ≥ 80% em `deile/core/models/`.

---

### **Fase 18 — Documentação**

**Objetivo**: docs sincronizadas com o novo arquitetural.

**Arquivos a modificar**
- `claude_dev/2_system_architecture_context.md`: adicionar diagrama da §2.1 + descrição.
- `README.md`: seção "Multi-Provider Support" com env vars necessárias.
- `CHANGELOG.md`: entrada nova `v5.1.0 - Multi-Provider Model Router`.
- `CLAUDE.md`: atualizar seção sobre `model_router` e remover referências a Gemini-only.

**Critérios de aceitação**
- Skill `DOC-HYGIENE` passa.
- Onboarding de novo dev consegue configurar OpenAI sozinho via README.

---

## 5. Estratégia de testes

### 5.1 Pirâmide

| Nível | Localização | Cobertura |
|---|---|---|
| Unit | `deile/tests/test_*.py` | Cada provider isolado com mocks SDK; ToolSchema translators; ModelCatalog parsing; CircuitBreaker state machine. |
| Integration | `deile/tests/integration/` | Roundtrip de tool call com cada provider; cascata de fallback; budget guard bloqueando. |
| Performance | `deile/tests/perf/` | Overhead do router (RNF-002); throughput de UsageRepository. |
| Compatibility | `deile/tests/compat/` | Sessão Gemini-only continua funcionando (RNF-001). |

### 5.2 Mocks dos SDKs

- `Anthropic`: mock `AsyncAnthropic.messages.create` retornando `Message(content=[TextBlock(text="..."), ToolUseBlock(id="x", name="bash", input={...})], stop_reason="tool_use", usage=Usage(input_tokens=10, output_tokens=20))`.
- `OpenAI`: mock `AsyncOpenAI.chat.completions.create` retornando `ChatCompletion(choices=[Choice(message=Message(content=None, tool_calls=[ToolCall(id="x", function=Function(name="bash", arguments='{"cmd": "ls"}'))]))], usage=Usage(...))`.
- `DeepSeek`: reaproveita mock OpenAI com `base_url` modificado.

### 5.3 Coverage gate

`pytest.ini` já configura `testpaths = deile/tests/`. Adicionar:
- `--cov=deile/core/models --cov=deile/storage/usage_repository --cov-fail-under=80`.

---

## 6. Estratégia de migração e rollback

### 6.1 Migração de sessões existentes

- Sessões DEILE v5.0 atuais usam `chat_session` interno do GeminiProvider (cache `_chat_sessions` em memória). Não são persistidas. **Resultado**: nenhuma migração de dados necessária — sessões expiram naturalmente entre execuções.
- O banco `deile_tasks.db` é mantido. Tabela `model_usage` é nova e independente das `tasks` — `CREATE TABLE IF NOT EXISTS` é seguro.

### 6.2 Feature flag

Adicionar em `model_providers.yaml`:
```yaml
feature_flags:
  use_legacy_gemini_only: false   # se true, ignora YAML e mantém comportamento v5.0
```

E em `deile.py`, se `feature_flags.use_legacy_gemini_only=true`, executar caminho antigo (registrar apenas `GeminiProvider` direto).

### 6.3 Rollback por fase

- Cada fase é commit separado.
- Fases 1-2 são puramente aditivas — rollback trivial.
- Fases 3-5 (novos providers) são isoladas — não afetam Gemini path.
- Fase 6 (refator Gemini) — ponto crítico. Manter branch `legacy/gemini-only` por 1 ciclo.
- Fases 7-10 (router) — a mudança de `task_model_mapping` é destrutiva. **Mitigação**: feature flag acima.
- Fase 16 (entry point) — rollback voltando o `if not get_api_key("gemini"): return False`.

---

## 7. Riscos transversais e decisões em aberto

### 7.1 Decisões que requerem confirmação do usuário

1. **Deprecar `deile/core/models/model_switcher.py` (~780 linhas) inteiramente?**
   - Recomendação: SIM. O catálogo está desatualizado (gpt-4 em vez de gpt-5.4), enum `ModelProvider` duplica o que vai estar em `provider_id`, e importa `infrastructure.monitoring.cost_tracker` (provavelmente quebrado).
   - Alternativa: manter shim de compatibilidade exportando wrappers para `register_switch_callback` (se algum código externo usa).

2. **Manter `gemini-1.5-pro-latest` ou subir para `gemini-2.5-*`?**
   - Atualmente `Settings.default_model_name = "gemini-1.5-pro-latest"` (settings.py:46) e `GeminiConfig.model_name = "gemini-2.5-flash-lite"` (manager.py:35) — já há divergência. Recomendação: usar `gemini-2.5-flash-lite` como default legado por ser mais barato e atual.

3. **Renomear `ModelSize` para algo mais explícito (`ModelCapacity`)?**
   - Recomendação: NÃO. Manter por retrocompat; novos códigos devem usar `ModelTier`.

4. **`/model use <provider>:<model_id>` é por sessão ou global?**
   - Recomendação: por sessão (`session.context_data`), para suportar múltiplas sessões CLI. Se persistente, salvar em `ConfigManager`.

5. **`cost_command.py` legado** depende de `infrastructure.monitoring.cost_tracker`. Reescrever ou apenas adaptar?
   - Recomendação: reescrever do zero usando `UsageRepository` (já que estamos refatorando custos).

### 7.2 Riscos técnicos

- **Divergência semântica `tool_use` Anthropic vs `function_call` Gemini vs `tool_calls` OpenAI**:
  - Anthropic: lista de blocos `tool_use`/`text` no `content` da mensagem do assistant. Próximo turno: `[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": ..., "content": ...}]}]`.
  - OpenAI: `choices[0].message.tool_calls` separado do `content`. Próximo turno: `[{"role": "tool", "tool_call_id": ..., "content": ...}]`.
  - Gemini: `Part.from_function_response(name, response)`.
  - **Mitigação**: cada provider encapsula esse loop internamente — agent só vê `(text, List[ToolResult], ModelUsage)`.

- **Role mapping**:
  - Anthropic: NÃO aceita role `system` em messages (vai em parâmetro separado `system=...`).
  - OpenAI: aceita `system` em messages.
  - Gemini: `system_instruction` separado.
  - **Mitigação**: cada provider transforma `ModelMessage` adequadamente.

- **Streaming de `tool_calls` em OpenAI**: arguments vêm fragmentados. `UnifiedStreamEvent.TOOL_USE_DELTA.arguments_json_delta` precisa concatenação até `TOOL_USE_END`.

- **Cache não disponível em todos os modelos**: `deepseek-reasoner` não tem function calling; `deepseek-v4-flash` cache é automático mas opaco. **Mitigação**: `capabilities` em `ModelHandle` filtra elegibilidade.

- **Nomes de modelos hipotéticos**: os IDs `claude-opus-4-7`, `gpt-5.3-codex`, `deepseek-v4-pro` etc. estão na especificação do usuário mas precisam ser **validados via context7** que correspondem aos IDs reais aceitos pelas APIs no momento da implementação. Se diferentes, ajustar `model_id` no YAML mantendo `display_name` e tier.

- **Janela de contexto / max_tokens output**: cada provider tem limites distintos. Adicionar `max_output_tokens` em `ModelHandle` e propagar para `chat_with_tools`.

- **Race conditions em `UsageRepository`**: escritas concorrentes em SQLite. Usar `_db_lock` (já é o pattern do `sqlite_task_manager`).

---

## 8. Checklist de pronto-para-merge

- [ ] `pytest deile/tests/` 100% verde.
- [ ] `pytest --cov=deile/core/models --cov-fail-under=80` passa.
- [ ] Smoke test manual com `ANTHROPIC_API_KEY` apenas: `python deile.py` boota, `/model list` mostra 3 modelos Anthropic + 6 indisponíveis com warning.
- [ ] Smoke test manual com `OPENAI_API_KEY` + `DEEPSEEK_API_KEY`: cascata `tier_1` task_optimized funciona (Anthropic disabled, OpenAI primary, DeepSeek fallback).
- [ ] Smoke test de erro: `ANTHROPIC_API_KEY=sk-invalid python deile.py`, pede algo, espera ver painel com JSON de erro 401 e fallback automático para próximo provider do tier (Regra 2).
- [ ] `/model cost` exibe agregação correta após 5 chamadas.
- [ ] `/model strategy cost_optimized` muda cascata em tempo real.
- [ ] Lint: `ruff check deile/` ou equivalente — zero erros.
- [ ] `radon cc deile/core/models/router.py` — complexidade ≤ B.
- [ ] `CHANGELOG.md` atualizado com entrada `v5.1.0`.
- [ ] `claude_dev/2_system_architecture_context.md` reflete novo diagrama.
- [ ] `README.md` documenta as 4 env vars suportadas.
- [ ] Sessão Gemini legado roda quando `GOOGLE_API_KEY` está presente e `model_providers.yaml.providers.gemini.enabled=true` (RNF-001).
- [ ] Benchmark de overhead do router < 50ms (RNF-002) registrado em `deile/tests/perf/`.
- [ ] `logs/router_events.jsonl` populado com eventos JSON estruturados (RNF-003).
- [ ] `requirements.txt` versionado com `anthropic>=0.40.0`, `openai>=1.50.0`.

---

### Critical Files for Implementation
Lista dos arquivos mais críticos onde a maior parte do trabalho de implementação acontecerá:

- deile/core/models/base.py
- deile/core/models/router.py
- deile/core/intent_analyzer.py
- deile/tools/registry.py
- deile/commands/builtin/model_command.py
- deile.py