# 06 — Memória

> Arquitetura híbrida de quatro camadas. Implementação em `deile/memory/`. Ponto de entrada: `MemoryManager` em `deile/memory/memory_manager.py`.

## Camadas e propósito

| Camada | Módulo | Propósito | Tempo de vida |
|---|---|---|---|
| Working | `working_memory.py` | Estado transitório dentro de uma tarefa/turno; cache de contexto ativo | TTL (segundos a minutos) |
| Episodic | `episodic_memory.py` | Log de eventos da sessão (interações, episódios) | Sessão / dias (`retention_days`) |
| Semantic | `semantic_memory.py` | Fatos e conhecimento que persistem entre sessões; embeddings | Persistente |
| Procedural | `procedural_memory.py` | Padrões aprendidos / habilidades extraídas de interações | Persistente, evolui |

## Configuração padrão (em `MemoryConfiguration`)

> Valores são padrão de `dataclass`. Ajustes devem passar pelo construtor de `MemoryManager` ou por configuração externa.

| Campo | Default | Descrição |
|---|---|---|
| `working_memory_size` | 8000 | Tamanho máximo da working memory |
| `working_memory_ttl` | 3600 (s) | TTL padrão das entradas em working memory |
| `max_episodes_per_session` | 1000 | Máximo de episódios por sessão |
| `episode_retention_days` | 30 | Retenção de episódios em dias |
| `enable_vector_store` | True | Habilita vetor para semantic |
| `vector_dimensions` | 768 | Dimensão dos embeddings semânticos |
| `similarity_threshold` | 0.7 | Threshold para busca semântica |
| `enable_pattern_learning` | True | Habilita aprendizado em procedural |
| `min_pattern_frequency` | 3 | Frequência mínima para promover padrão |
| `pattern_confidence_threshold` | 0.8 | Confiança mínima para reuso |
| `consolidation_interval` | 3600 (s) | Intervalo do consolidator |
| `auto_cleanup_enabled` | True | Cleanup automático |
| `memory_pressure_threshold` | 0.85 | Acima disso, otimização força execução |

## API pública (verificada nos módulos)

### `MemoryManager`

| Método | Propósito |
|---|---|
| `await initialize()` | Inicializa o manager e as camadas |
| `await store_interaction(...)` | Caminho conveniente para a maioria dos casos |
| `await retrieve_context(...)` | Busca paralela em todas as camadas |
| `await learn_from_feedback(...)` | Ajusta camadas com feedback |
| `await get_memory_usage()` | Métricas |
| `await optimize_memory(force=False)` | Dispara otimização imediata |
| `await shutdown()` | Encerra o manager |

### `WorkingMemory`

| Método | Propósito |
|---|---|
| `await store(...)` / `await store_interaction(...)` | Armazena entrada com TTL |
| `await retrieve(entry_id)` | Recupera entrada |
| `await search(...)` | Busca |
| `await update_with_feedback(...)` | Atualiza com feedback |
| `await clear_type(entry_type)` | Limpa por tipo |
| `await get_stats()` / `await shutdown()` | Métricas / encerramento |

### `EpisodicMemory`

| Método | Propósito |
|---|---|
| `await store_episode(...)` | Persiste um episódio |
| `await search_episodes(...)` | Busca em episódios |
| `await get_stats()` / `await shutdown()` | Métricas / encerramento |

### `SemanticMemory`

| Método | Propósito |
|---|---|
| `await store_knowledge(knowledge: dict)` | Persiste conhecimento estruturado |
| `await search_knowledge(query, max_results=10)` | Busca semântica |
| `await store_correction(interaction_id, correction_data)` | Registra correção |
| `await get_stats()` / `await shutdown()` | Métricas / encerramento |

### `ProceduralMemory`

| Método | Propósito |
|---|---|
| `await analyze_interaction(pattern_data: dict)` | Atualiza padrões com nova interação |
| `await get_relevant_patterns(query)` | Recupera padrões relevantes |
| `await update_pattern_effectiveness(...)` | Ajusta confiança/efetividade |
| `await get_stats()` / `await shutdown()` | Métricas / encerramento |

### `MemoryConsolidator`

| Aspecto | Detalhe |
|---|---|
| Função | Coordena otimização entre as camadas |
| Cadência | Loop configurável via `consolidation_interval` |
| Pressão | Respeita `memory_pressure_threshold` para forçar execução |

## Persistência em disco

`MemoryManager.__init__` cria `memory_dir` (default `deile/memory/storage/`) com subdiretórios:

| Subdiretório | Camada |
|---|---|
| `episodes/` | `EpisodicMemory` |
| `semantic/` | `SemanticMemory` |
| `patterns/` | `ProceduralMemory` |

> Working memory é mantida em RAM (sem subdiretório).

## Regras inegociáveis ao integrar com memória

| Regra | Detalhe |
|---|---|
| Sempre `await` | Toda escrita usa `await` |
| Escolher por propósito | Não por conveniência (ver tabela das camadas acima) |
| Sem segredos/PII | Em qualquer camada — para dados sensíveis use o `SecretsScanner` antes (ver [`08-SEGURANCA.md`](08-SEGURANCA.md)) |
| Confirmar assinaturas | Abrir o módulo correspondente antes de chamar — o catálogo acima é guia, não contrato congelado. As implementações evoluem |
| Sem globals | Não usar globals de módulo nem atributos de classe como cache cross-turn — usar a camada apropriada |
