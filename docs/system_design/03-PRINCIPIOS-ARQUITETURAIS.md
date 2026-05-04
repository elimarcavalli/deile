# 03 — Princípios arquiteturais inegociáveis

> Regras que governam toda contribuição ao código. Aplicação prática (templates de código) em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md). Workflow operacional em [`11-WORKFLOW-DESENVOLVIMENTO.md`](11-WORKFLOW-DESENVOLVIMENTO.md).

## Índice de gatilhos rápidos

| Situação que você está prestes a escrever | Aplicar princípio |
|---|---|
| Nova classe que herda de `Tool`, `SlashCommand` ou `Parser` | Registry Pattern + Tool/Command/Parser System |
| Função que faz I/O (arquivo, rede, DB) | Async-First |
| Nova dependência externa em `core/` ou `orchestration/` | Hexagonal — colocar adapter em `infrastructure/` |
| Código que recebe path/comando/query do usuário | Security-First |
| Estado que precisa sobreviver entre turnos ou sessões | Memória de quatro camadas (escolha a camada certa) |
| `try: ... except Exception:` | Error Handling tipado |
| Leitura de configuração nova | Configuração centralizada (`Settings` singleton) |
| Componente para ser plugado/descoberto | Registry + Extensibility |
| Operação multi-step que pode falhar no meio | Orquestração com rollback |

> Se múltiplas linhas se aplicam, **aplique todas**. Default ao escrever em `deile/`: **Async-First + Registry + princípio mais próximo da responsabilidade do subpacote**.

---

## 1. Async by design

| Regra | Detalhe |
|---|---|
| Toda I/O é `async` | Sem `requests`, `time.sleep`, `open()` síncrono ou driver de DB síncrono dentro de `async def` |
| Concorrência | `asyncio.gather(...)` para I/O independente concorrente |
| Cleanup | `async with` para recursos que devem ser liberados |
| Tools síncronas | Usam `SyncTool` (`deile/tools/base.py`), que envolve em `asyncio.to_thread` automaticamente |
| Auto-revisão | Procurar awaitables sem `await` — falha silenciosa por compressão é comum |

## 2. Hexagonal / Clean Architecture

| Regra | Onde se aplica |
|---|---|
| Núcleo livre de SDK externo | `deile/core/`, `deile/orchestration/`, `deile/memory/` NÃO importam SDKs externos diretamente |
| Adapter externos | Vivem em `deile/infrastructure/` ou em providers concretos em `deile/core/models/` |
| Validação | Pydantic v2 para dados e contratos |

## 3. Registry Pattern

| Regra | Detalhe |
|---|---|
| Componentes plugáveis | Tools, Commands, Parsers e Personas são descobríveis pelo seu registry |
| Auto-discovery de tools | `ToolRegistry.auto_discover()` cobre conjunto-padrão (`file_tools`, `execution_tools`, `search_tool`, `bash_tool`, `slash_command_executor`) |
| Demais tools | Registro explícito via `register_tool(tool, aliases)` (helper em `deile/tools/registry.py`) ou `registry.register(tool)` |
| Sem dispatch por tipo | Não usar `isinstance(...)` em chains — o registry é o ponto único de resolução |

## 4. Memória em quatro camadas

> Detalhamento em [`06-MEMORIA.md`](06-MEMORIA.md).

| Regra | Detalhe |
|---|---|
| Sem globals/atributos | Não armazenar estado cross-turn em globals de módulo nem em atributos de classe |
| Escolher por propósito | Working = TTL transitório, Episodic = eventos da sessão, Semantic = fatos persistentes, Procedural = padrões aprendidos |
| Sem segredos/PII | Em **qualquer** camada |
| Sempre `await` | Toda escrita é assíncrona |
| Confirmar assinatura | Abrir o módulo da camada — não inventar nomes de método |

## 5. Security-First

> Detalhes em [`08-SEGURANCA.md`](08-SEGURANCA.md).

| Regra | Detalhe |
|---|---|
| Permissão antes da ação | Verificar via `PermissionManager` antes de qualquer ação privilegiada |
| Sanitização | Entrada do usuário sempre sanitizada antes de chegar a shell/SQL/filesystem |
| Audit tipado | Logar via `AuditLogger` com `AuditEvent` — nunca formato livre |
| Não logar segredos | Não logar segredos nem corpos de request inteiros |

## 6. Error Handling

| Regra | Detalhe |
|---|---|
| Sem `bare except` | Capturar exceções específicas |
| `except Exception` | Sempre logar com contexto e re-raise quando for runtime fatal |
| `asyncio.CancelledError` | Nunca capturar sem re-raise |
| Erros de domínio | Subclasses de `DEILEError` (`deile/core/exceptions.py`) |

## 7. Configuração centralizada

| Regra | Detalhe |
|---|---|
| Acessor único | Toda leitura passa por `get_settings()` (`deile/config/settings.py`) ou `ConfigManager` (`deile/config/manager.py`) |
| Proibido | Ler `os.environ` ou YAML diretamente em código de domínio |
| Flag de bootstrap | `use_legacy_gemini_only` em `deile/config/model_providers.yaml` decide o caminho de bootstrap |

## 8. Tool Design

| Regra | Detalhe |
|---|---|
| Single responsibility | Uma tool = um propósito |
| Schema | `ToolSchema` em `deile/tools/base.py` com conversores `to_anthropic_tool`, `to_openai_function`, `to_gemini_function` |
| Retorno | `Tool.execute()` retorna `ToolResult` — nunca lança fora da execução; mapear exceções a `ToolResult.error_result(...)` |
| Classificação | Categorizar com `ToolCategory` e classificar risco com `SecurityLevel` |

## 9. Orquestração com rollback

| Regra | Detalhe |
|---|---|
| Rollback handler | `WorkflowExecutor`/`PlanManager` implementam handler quando a etapa é reversível |
| Progresso | Operações multi-step emitem progresso pelo event bus |
| Ações de risco | Passam por `ApprovalSystem` antes de executar |

## 10. Extensibilidade

| Regra | Detalhe |
|---|---|
| Open/Closed | Adicionar tool/comando/parser/persona NÃO exige mudança no núcleo |
| Personas | Comportamento via Markdown + YAML, sem mudança de Python |
| Plugins | Ciclo de vida via `PluginManager` + `hot_loader` (`PluginSandbox` é skeleton — não isola; ver issue #54) |

## 11. Observabilidade

| Regra | Componente |
|---|---|
| Logger principal | `get_logger()` em `deile/storage/logs.py` |
| Debug específico | `deile/storage/debug_logger.py` |
| Eventos | `EventBus` em `deile/events/event_bus.py` |
| Uso/custo | `UsageRepository` (SQLite); enforcement em `BudgetGuard` |

## 12. Testes

| Regra | Detalhe |
|---|---|
| Configuração | `pytest.ini` define `testpaths=deile/tests` e `--cov-fail-under=80` |
| Async | `asyncio_mode=auto` — testes async não precisam de `@pytest.mark.asyncio` |
| Markers | Registrados em `pytest.ini` sob `markers:`; `--strict-markers` ativo (registrar antes de usar) |

## 13. Persona

| Regra | Detalhe |
|---|---|
| Instruções | `deile/personas/instructions/*.md` — editar Markdown para ajustar comportamento, não Python |
| Capacidades e preferências | `deile/personas/library/*.yaml` e `deile/config/persona_config.yaml` |

## 14. Dependências resolvíveis

| Regra | Detalhe |
|---|---|
| Toda extra em `pyproject.toml` deve resolver em ambiente limpo | Sem nomes nominais que apontem para pacotes não publicados; usar git URL ou local path |
| Sem PyPI privado | Não declarar deps que dependam de PyPI privado/credenciado |
| CI smoke obrigatório | Toda PR que toca `pyproject.toml` ou `[project.optional-dependencies]` deve passar por job que faz `pip install -e ".[<extra>]"` em venv limpo |
| Doc-instalação consistente | Todo `pip install <X>[Y]` em CLAUDE.md/README precisa corresponder a uma extra que resolve sem intervenção manual |
