# 03 — Princípios arquiteturais inegociáveis

> Regras que governam toda contribuição ao código. Aplicação prática (templates de código) em [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md). Workflow operacional em [`11-WORKFLOW-DESENVOLVIMENTO.md`](11-WORKFLOW-DESENVOLVIMENTO.md).

## Índice de gatilhos rápidos

| Situação que você está prestes a escrever | Aplicar princípio |
|---|---|
| Nova classe que herda de `Tool`, `SlashCommand` ou `Parser` | Registry Pattern + Tool/Command/Parser System |
| Função que faz I/O (arquivo, rede, DB) | Async-First |
| Nova dependência externa em `core/` ou `orchestration/` | Hexagonal — colocar adapter em `infrastructure/` |
| Código que recebe path/comando/query do usuário | Security-First |
| Construção de UI com caixa/painel/largura visível ao usuário | UI adaptativa (Panel/Rule sem `width=N`); ver princípio 15 |
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
| Configuração | `pytest.ini` define `testpaths=deile/tests`; **`--cov-fail-under` está no CI** (`ci.yml` job `test`, gate real `--cov-fail-under=85`) — ausente do `pytest.ini` para não bloquear runs locais de subconjunto (invariante verificado por `scripts/validate_doc_consistency.py`) |
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

## 15. UI: adaptação a resize do terminal

> Estabelecido pela issue #307. **Regra raiz: DEILE tem layout dinâmico em todos os seus recursos** — toda surface UI consulta `console.width` no momento do render, nenhuma largura é literal. Vale para `Panel`, `Table`, `Rule`, `Live` e qualquer construção de moldura.
>
> **Resize-em-tempo-real:** surfaces críticas (welcome screen, comandos com tabelas pesadas tipo `/status`, `/logs`, `/cost`) embrulham seu conteúdo em `rich.live.Live` por alguns segundos via `deile.ui.dynamic_render.live_for`. Durante esse período, Rich captura `SIGWINCH` e re-renderiza cada frame com a largura corrente — se o usuário redimensiona, o painel adapta sem quebrar bordas. Após o tempo configurado, o último frame fica no scrollback (limitação fundamental abaixo). A solução enterprise definitiva (Textual framework, todo o CLI dentro de um App reativo) está descrita em issue separada.

| Regra | Detalhe |
|---|---|
| Construções com largura derivada de texto | Proibido. Não calcular `inner_w = max(len(...), ...)` para desenhar `╔══╗` manualmente — trava a largura no momento da renderização |
| Usar primitivas adaptativas do Rich | `Panel`, `Rule`, `Table` (sem `width=N`) — consultam `console.width` lazy via `os.get_terminal_size()` em cada render |
| **`Table.add_column(...)` sem `width=<int>`** | Proibido literal. Rich auto-calcula a largura ótima por coluna em cada render usando `console.width` corrente. Permitido: `max_width=N` (teto, Rich pode encolher), `min_width=N` (piso), `ratio=N` (proporção), `width=None`. Verificado automaticamente em `deile/tests/commands/test_table_widths_adaptive.py` |
| `Console()` sem `width=` | Não passar `width=N` para `rich.console.Console` salvo em testes ou consoles internos de captura (`.capture()` pra normalizar texto, nunca exibido ao usuário) — Rich precisa detectar a largura corrente do terminal |
| Live region | Já adapta: `_render_live` chama `live.update(...)` por evento, cada `_compose` usa `console.width` corrente |
| **Conteúdo já no scrollback NÃO reflowa** | Limitação fundamental de terminais. Uma vez que texto ANSI é commitado via `console.print()`, ele vive no buffer do emulador — não é mais nosso |
| Sem `signal.SIGWINCH` | Não cross-platform (Windows não tem); o ganho de reagir a resize ativo é marginal e o custo (signal+asyncio+Live) é alto |
| Sem `clear()` + replay | Destruiria scrollback histórico — UX regression |

**O que adapta vs. o que NÃO adapta:**

| Comportamento | Adapta? | Por quê |
|---|---|---|
| **Welcome em tempo real durante os primeiros segundos** | ✅ Sim, em tempo real | Embrulhado em `Live` via `live_for(duration_s=6)` |
| **Tabela de comando pesado opt-in (`/status`, `/logs`, `/cost`, ...) em tempo real** | ✅ Sim, em tempo real | `cli.py` seta `metadata['live_render']=True`; `display_response` chama `live_for(duration_s=2.5)` |
| Próxima chamada de `show_welcome` / `display_error` / `display_stats` após resize | ✅ Sim (próximo render) | Panel/Table consultam `console.width` corrente |
| Tabela de qualquer comando slash após resize | ✅ Sim (próximo render) | Colunas sem `width=N`; Rich auto-calcula a partir de `console.width` em cada render |
| Live region durante streaming após resize | ✅ Sim, em tempo real | `_compose` reusa `console.width` em cada frame |
| `prompt_toolkit` input area | ✅ Sim | Lida com SIGWINCH internamente |
| Markdown / Panel / ASCII art já no scrollback (depois do Live encerrar) | ❌ Não | Texto ANSI estático fora do controle da aplicação — solução final via Textual em issue follow-up |
