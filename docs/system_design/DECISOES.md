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
