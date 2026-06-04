# 07 — Integrações com Modelos LLM

> Multi-provider, roteamento por tier, circuit breaker, budget. Implementação em `deile/core/models/`. Catálogo concreto de providers e modelos em [`deile/config/model_providers.yaml`](../../deile/config/model_providers.yaml).

## Provedores suportados

| Provider | Provider ID | SDK | Variável de ambiente | Provider class |
|---|---|---|---|---|
| Anthropic | `anthropic` | `anthropic` (SDK oficial) | `ANTHROPIC_API_KEY` | `deile/core/models/anthropic_provider.py:AnthropicProvider` |
| OpenAI | `openai` | `openai` | `OPENAI_API_KEY` | `deile/core/models/openai_provider.py:OpenAIProvider` |
| DeepSeek | `deepseek` | `openai` (compat layer; `base_url` customizada em `model_providers.yaml`) | `DEEPSEEK_API_KEY` | `deile/core/models/deepseek_provider.py:DeepSeekProvider` (subclassa `OpenAIProvider`) |
| Gemini | `gemini` | `google-genai` (novo SDK) | `GOOGLE_API_KEY` | `deile/core/models/gemini_provider.py:GeminiProvider` |

> Pelo menos uma `*_API_KEY` precisa estar definida no ambiente. Se nenhuma estiver, a CLI sai com mensagem de erro instruindo qual variável definir.

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
