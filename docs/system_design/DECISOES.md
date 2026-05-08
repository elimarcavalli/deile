# Registro de Decisões Arquiteturais

> Detalhe completo de cada decisão. A tabela-resumo (índice) vive em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md). Decisões são **contratos vivos**: quando o design evolui, atualizar a decisão original in-place e adicionar entrada em `### Histórico`.

> Estas decisões foram **inferidas a partir do código atual** durante a migração inicial deste System Design. Datas são as do `git log` que introduziu cada decisão; este arquivo não duplica datas — consulte o histórico do git.

---

## Decisão #1 — CLI single-binary com bootstrap condicional de providers

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura |
| Decisão | Ponto de entrada único em `deile.py`. Suporta REPL interativo (`DeileAgentCLI.run_interactive`) ou one-shot (`_run_oneshot`) decidido pela presença de argumentos posicionais. O bootstrap registra apenas providers cuja `*_API_KEY` está definida, com fallback `use_legacy_gemini_only` controlado por `model_providers.yaml` |
| Evidência | `deile.py` (`main`, `DeileAgentCLI.initialize`, `_run_oneshot`, `_use_legacy_gemini_only`, `_bootstrap_legacy_gemini`) |
| Motivação | Operação local sem ortogonalidade entre fluxos de CLI; bootstrap condicional evita exigir todas as credenciais |

---

## Decisão #2 — Pelo menos uma chave de API de LLM é requerida no startup

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 09-Configuração |
| Decisão | Se `bootstrap_providers` retornar lista vazia, a CLI exibe erro listando todas as variáveis aceitáveis e sai sem subir o agente. Vale para o modo interativo e o one-shot |
| Evidência | `deile.py` (`DeileAgentCLI.initialize` e `_run_oneshot`) |
| Motivação | Falha rápida com mensagem clara, evitando estado parcial em runtime |

---

## Decisão #3 — Registry Pattern para Tools, Commands, Parsers, Personas

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes |
| Decisão | Quatro registries singleton: `ToolRegistry`, `CommandRegistry`, `ParserRegistry`, `PersonaManager`. Tools suportam `auto_discover()` para um conjunto fixo de módulos; o restante é registrado explicitamente via `register_tool(tool, aliases)` (helper de função, **não** decorator) |
| Evidência | `deile/tools/registry.py` (`ToolRegistry.auto_discover` e helper `register_tool` em linha 647), `deile/commands/registry.py`, `deile/parsers/registry.py`, `deile/personas/manager.py` |
| Motivação | Extensão sem modificação do núcleo; geração automática de declarações para function calling |

### Histórico

| Sessão | Mudança |
|---|---|
| Sessão inicial | Descoberta de que `@register_tool` decorator (mencionado em docs antigos) **não existe** — apenas a função helper. Decisão atualizada para refletir o real |

---

## Decisão #4 — Async/await obrigatório em toda I/O

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 03-Princípios |
| Decisão | Todo I/O (arquivo, rede, DB) é `async`. Tools síncronas legítimas usam `SyncTool`, que envolve em `asyncio.to_thread`. `pytest.ini` configura `asyncio_mode=auto` |
| Evidência | `deile/tools/base.py:SyncTool.execute`, `pytest.ini` |
| Motivação | Manter responsividade da CLI durante operações longas (function calling, leitura de arquivo, integrações) |

---

## Decisão #5 — Arquitetura hexagonal — núcleo livre de SDKs externos

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 03-Princípios |
| Decisão | O núcleo (`deile/core/`, `deile/orchestration/`, `deile/memory/`) não importa SDKs externos diretamente. Adapters vivem em `deile/infrastructure/` e providers concretos em `deile/core/models/`. Dados validados por Pydantic v2 |
| Evidência | Estrutura de diretórios e ausência de imports de `anthropic`, `openai`, `google.genai` em `deile/core/agent.py`, `deile/orchestration/`, `deile/memory/` |
| Motivação | Trocar provider/SDK sem reescrever o núcleo |

---

## Decisão #6 — Memória em quatro camadas

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 06-Memória |
| Decisão | Camadas: Working (TTL, RAM), Episodic (persistente, retenção em dias), Semantic (persistente, embeddings), Procedural (padrões aprendidos). Coordenadas por `MemoryManager` com consolidador em background |
| Evidência | `deile/memory/memory_manager.py:MemoryConfiguration`, módulos `working_memory.py`, `episodic_memory.py`, `semantic_memory.py`, `procedural_memory.py`, `memory_consolidation.py` |
| Motivação | Separar contexto efêmero, log de sessão, conhecimento estável e padrões reutilizáveis |

---

## Decisão #7 — Multi-provider com router legado e router por tier

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | Coexistem `ModelRouter` (legado, por priority) e `TierRouter` (cascata por tier com `RoutingPolicy` e `CircuitBreaker`). Bootstrap registra cada handle (`provider:model_id`) em ambos para compatibilidade |
| Evidência | `deile/core/models/router.py`, `deile/core/models/tier_router.py`, `deile/core/models/bootstrap.py` |
| Motivação | Permitir migração gradual sem quebrar caminhos legados; `TierRouter` é o caminho moderno |

---

## Decisão #8 — Circuit breaker por provider e budget por sessão/diário/mensal

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | Falhas consecutivas abrem o breaker do provider; janela de cooldown e meio-aberto antes de fechar. Budget enforcement em três janelas (sessão, dia por provider, mês por provider). Limites em `model_providers.yaml` |
| Evidência | `deile/core/models/tier_router.py` (`_ProviderBreaker`, `CircuitBreaker`, `BreakerState`), `deile/storage/usage_repository.py` (`BudgetGuard`, `BudgetExceeded`), `deile/config/model_providers.yaml` (seções `circuit_breaker` e `budget`) |
| Motivação | Resiliência (não martelar provider em falha) e proteção de custo |

---

## Decisão #9 — Permissões rule-based + audit logging tipado

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `PermissionManager` baseado em `PermissionRule`/`PermissionLevel`/`ResourceType`. `AuditLogger` recebe `AuditEvent` tipado com `AuditEventType` e `SeverityLevel`. Helpers prontos para os casos mais comuns (`log_permission_check`, `log_secret_detection`, `log_tool_execution`, etc.) |
| Evidência | `deile/security/permissions.py`, `deile/security/audit_logger.py`, `config/permissions.yaml` |
| Motivação | Decisões de segurança auditáveis e configuráveis sem mudança de código |

---

## Decisão #10 — Sistema de aprovação por nível de risco em planos

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `ApprovalSystem` em `deile/orchestration/approval_system.py` recebe `ApprovalRequest` com `RiskLevel` e expira após janela. `PlanManager` invoca aprovação antes de steps de risco |
| Evidência | `deile/orchestration/approval_system.py`, `deile/orchestration/plan_manager.py:_perform_security_checks` |
| Motivação | Operações de risco precisam de gate explícito |

---

## Decisão #11 — `Settings` como singleton

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 09-Configuração |
| Decisão | Acesso a `Settings` exclusivamente via `get_settings()`. Nunca instanciar `Settings()` diretamente. `update_settings(**kwargs)` para mudanças in-place; `reset_settings()` para testes |
| Evidência | `deile/config/settings.py` |
| Motivação | Estado de configuração único para evitar divergência entre componentes |

---

## Decisão #12 — Personas via Markdown + YAML

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes |
| Decisão | Instruções da persona ficam em `deile/personas/instructions/<id>.md`; capacidades e preferências em `deile/personas/library/<id>.yaml`; mapeamento e default em `deile/config/persona_config.yaml`. Hot-reload é opcional via `PersonaManager.initialize(enable_hot_reload=True)` |
| Evidência | `deile/personas/manager.py`, `deile/personas/loader.py`, `deile/config/persona_config.yaml` |
| Motivação | Mudar comportamento do agente sem mudar Python |

---

## Decisão #13 — Hot-reload via `watchdog`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 09-Configuração |
| Decisão | `ConfigManager` e `PluginManager.hot_loader` instalam observers do `watchdog` para detectar mudanças em diretórios de configuração e plugins. Quando `watchdog` não está disponível, hot-reload é silenciosamente desativado (warning no log) |
| Evidência | `deile/config/manager.py` (lazy import de `watchdog`), `deile/plugins/hot_loader.py` |
| Motivação | Iteração rápida sem reiniciar a CLI |

---

## Decisão #14 — Persistência em SQLite

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 06-Memória + 07-Integrações LLM |
| Decisão | SQLite usado para: (a) repositório de uso/custo (`deile/storage/usage_repository.py`); (b) gerenciamento de tarefas (`deile/orchestration/sqlite_task_manager.py`); (c) camadas de memória persistentes (episodic/semantic/procedural usam diretórios sob `memory_dir` — combinação de SQLite e arquivos, conforme implementação de cada camada) |
| Evidência | imports em `deile/storage/usage_repository.py`, `deile/orchestration/sqlite_task_manager.py` |
| Motivação | Persistência leve, ACID, sem dependência de servidor |

---

## Decisão #15 — Streaming-first na CLI interativa

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 05-Fluxo |
| Decisão | A CLI tenta primeiro `process_input_stream(...)` quando `Settings.streaming_enabled` é True. Caminho legado (`process_input`) continua disponível e é o usado no modo one-shot |
| Evidência | `deile.py:DeileAgentCLI.run_interactive` (verificação `streaming_enabled = getattr(self.settings, "streaming_enabled", True)`) |
| Motivação | Resposta progressiva melhora a percepção de latência; tools/eventos podem ser renderizados conforme chegam |

---

## Decisão #16 — Feature flag `use_legacy_gemini_only`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | Em `model_providers.yaml`, a flag `feature_flags.use_legacy_gemini_only=true` desvia o startup para `_bootstrap_legacy_gemini` em `deile.py`, registrando apenas o `GeminiProvider` no router legado |
| Evidência | `deile.py:_use_legacy_gemini_only`, `_bootstrap_legacy_gemini`; `deile/config/model_providers.yaml:feature_flags` |
| Motivação | Caminho de fallback / depuração quando a stack multi-provider precisa ser desabilitada |

---

## Decisão #17 — Separação `deile` / `deilebot` e protocolo HTTP local para a flecha reversa

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 04-Componentes, 08-Segurança |
| Decisão | A árvore `deilebot/` foi extraída para um repositório separado (`elimarcavalli/deilebot`) com `pyproject.toml` próprio. A comunicação `deile → deilebot` (a flecha reversa) usa **HTTP local** (aiohttp.web em `127.0.0.1`, Bearer token), não importação in-process nem outbox SQLite. O cliente publicável `deilebot` (httpx + pydantic) é a única superfície que a CLI da DEILE consome via extra opcional `bot` |
| Evidência | `deilebot/runtime/control_plane/`, `deilebot/deilebot/`, `deile/integrations/bot/`, `deile/tools/messaging/`, `pyproject.toml` (extra `bot = ["deilebot @ git+https://github.com/elimarcavalli/deilebot.git@main"]`), `deilebot/pyproject.toml` |
| Motivação | (1) Daemons de chat têm ciclo de vida independente da CLI; in-process forçaria subir o bot toda vez que a CLI abrisse e dificultaria deploy. (2) Outbox SQLite introduziria latência e perderia feedback síncrono (msg_id, falhas Discord). (3) HTTP em loopback dá ack imediato, isola repos, permite versionar contrato via tag, e reaproveita um cliente publicável sem expor a stack do bot. (4) Bearer auth + bind 127.0.0.1 fechá fecha a porta para qualquer processo fora da máquina |
| Alternativas consideradas | (a) **In-process**: reprovado — junta ciclos de vida e complica testes. (b) **Outbox SQLite**: reprovado — sem feedback síncrono, latência de polling. (c) **gRPC**: reprovado — overhead de proto + nada que justifique a complexidade para 9 endpoints |
| Trade-offs | Operação local fica dependente de o daemon estar de pé. Mitigado: tools só registram quando o daemon foi configurado; offline → `ToolResult.error_result(code="BOT_UNREACHABLE")` tipado, não stack trace |
| Impacto breaking | Os extras `discord/telegram/whatsapp/meta/all-bots` e o console-script `deilebot` **saíram** do `pyproject.toml` da DEILE. Quem instalava `pip install deile[discord]` agora deve usar `pip install deilebot[discord]` |

---

## Decisão #18 — Hash sharding para execução paralela de monitores

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 03-Princípios, 02-Arquitetura |
| Decisão | Cada instância do `PipelineMonitor` tem uma identidade (`MonitorIdentity`) composta por `monitor_id`, `shard_index` e `shard_count`. O método `owns(key)` computa `SHA-256(key) % shard_count == shard_index` para decidir se aquela instância deve processar um dado issue/PR. Dois monitores com `shard_count=2` e `shard_index=0,1` dividem o trabalho de forma determinística e sem comunicação entre si. A identidade padrão (`monitor_id=default`, `shard_count=1`) mantém comportamento backwards-compatible de monitor único |
| Evidência | `deile/orchestration/pipeline/identity.py:MonitorIdentity.owns`, `monitor.py:_review_one_new_issue` |
| Motivação | Permitir escalar o pipeline horizontalmente (N máquinas ou N processos) sem um coordenador central, sem banco de dados compartilhado e sem mudança de protocolo com o GitHub. O `~batch:` label garante que dois monitors não processem a mesma issue simultaneamente |

---

## Decisão #19 — Cron genérico separado do scheduler do pipeline

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes |
| Decisão | Existem dois mecanismos de agendamento com propósitos distintos: (a) `ScheduleStore` / `Schedule` / `RecurringEntry` / `OneshotEntry` em `orchestration/pipeline/scheduler.py` — YAML por monitor, controla *quando* cada estágio do pipeline dispara (review/implement/pr_review); (b) `CronStore` / `CronEntry` / `CronRunner` em `cron/` — SQLite global, agenda *prompts naturais* do usuário para execução futura em uma turn do agente. Os dois são independentes: o pipeline scheduler determina atividade do monitor; o cron runner dispara texto arbitrário que o usuário queira agendar |
| Evidência | `deile/orchestration/pipeline/scheduler.py`, `deile/cron/store.py`, `deile/cron/runner.py` |
| Motivação | Misturar os dois num único mecanismo forçaria o cron a conhecer semântica de pipeline (review/implement/pr_review) e impediria que o cron fosse usado para tarefas não relacionadas ao pipeline. A separação mantém responsabilidades únicas (SRP) |

---

## Decisão #20 — Strip de `ANTHROPIC_API_KEY` no subprocess do Claude Code

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `ClaudeDispatcher` tem flag `prefer_subscription_auth=True` (default). Quando ativa, o env passado ao subprocess `claude -p <prompt>` tem `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` e `ANTHROPIC_BEARER_TOKEN` removidos. O subprocess `claude` cai então para autenticação por assinatura Claude Pro/Max do operador |
| Evidência | `deile/orchestration/pipeline/claude_dispatcher.py:ClaudeDispatcher._build_env`, atributo `_STRIP_KEYS` |
| Motivação | O agente DEILE tipicamente roda com uma `ANTHROPIC_API_KEY` de API (paga por token). O Claude Code usado para implementação no pipeline deve faturar contra a assinatura do operador, não contra a mesma key do DEILE. Sem o strip, os subprocessos consumiriam tokens da key do DEILE, gerando custo inesperado e potencialmente excedendo o budget |

---

## Decisão #21 — Pipeline silenciosamente quebrado (#129): schedule incompleto + fallback gaps

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 02-Arquitetura, 09-Configuração |
| Decisão | O schedule padrão (`config/pipeline_schedule_default.yaml`) deve incluir todos os 4 estágios (classify, review, implement, pr_review). Adicionalmente, o `tick()` executa fallback legacy para qualquer estágio habilitado que tenha entradas `recurring` no schedule mas não inclua aquele estágio específico. Estágios ausentes do schedule não ficam silenciosos. |
| Evidência | `config/pipeline_schedule_default.yaml` (4 entradas); `deile/orchestration/pipeline/monitor.py:tick()` (fallback block com `scheduled_actions` check) |
| Motivação | O bug em #129: o schedule só tinha `review` → classify/implement/pr_review nunca rodavam, mas o operador via `pipeline running` e não recebia nenhum erro. A dupla solução (schedule completo + fallback) é defense-in-depth — se o operador editar o schedule e remover uma entrada, o stage ainda roda; não silencia. |

---

## Decisão #22 — Atomicidade de Stage 1 e rollback para nova em caso de falha

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 03-Princípios (Orquestração com rollback) |
| Decisão | Stage 1 (`_review_one_new_issue`) usa try/except/finally onde `review_failed = True` no except aciona, no finally, uma transição de rollback `em_revisao → nova`. Isso garante que uma falha (gh error, callback error, etc.) não deixa a issue presa em `~workflow:em_revisao`. |
| Evidência | `deile/orchestration/pipeline/monitor.py:_review_one_new_issue()` |
| Motivação | Issues presas em `em_revisao` bloqueiam o monitor indefinidamente (a issue nunca é reclamável por outro agente). O rollback é best-effort (o `try` interno no finally não propaga) mas garante a melhor tentativa de desbloquear. |

---

## Decisão #23 — Batch ID derivado do número (não do título) para eliminar colisões

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 02-Arquitetura |
| Decisão | `compute_batch_id_for_number(kind, number)` gera o batch ID como SHA-256 de `"kind:number"` (e.g. `"issue:42"`). Substitui `compute_batch_id(title)` que usava o título — sujeito a colisões entre issues com mesmo título (duplicatas, re-criações). |
| Evidência | `deile/orchestration/pipeline/github_client.py:compute_batch_id_for_number`, `claim_with_batch` |
| Motivação | Dois issues com títulos idênticos receberiam o mesmo batch ID, permitindo que um monitor claim a issue "errada" silenciosamente. Com o número, o ID é sempre único dentro do repositório. |

---

## Como adicionar uma nova decisão

| # | Passo |
|---|---|
| 1 | Verificar se o tema **não está coberto** por nenhuma decisão existente. Se está, **atualizar** a decisão original in-place e adicionar entrada em `### Histórico` |
| 2 | Se for genuinamente nova: próximo número sequencial; classificar a versão (V1/V2/V3) conforme o impacto |
| 3 | Atualizar a tabela-resumo em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md) |
| 4 | Documentar: **Decisão**, **Evidência** (arquivo:linha), **Motivação** |
| 5 | Propagar: editar os documentos de pilar afetados (sem duplicar texto — eles devem **referenciar** esta decisão) |

## Proibido

| Regra | Detalhe |
|---|---|
| Decisão "Modifica #X" | Vá lá e ATUALIZE a #X — nunca crie nova decisão referenciando outra |
| Texto desatualizado | Se o design mudou, o texto da decisão muda junto |
| Decisão = contagem | Contagens pertencem ao documento dono em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md) |
