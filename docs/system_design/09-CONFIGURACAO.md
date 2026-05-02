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

> Regra: **nunca instanciar `Settings()` diretamente**. Sempre via `get_settings()`.

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
| `autonomous_agent.yaml` | Profile aplicável a `ConfigManager` |
| `enterprise.yaml` | Profile aplicável a `ConfigManager` |

> São profiles para alterar comportamento sem editar os YAMLs base.

## Arquivos em `config/` (raiz)

| Arquivo | Responsabilidade |
|---|---|
| `permissions.yaml` | Regras default de `PermissionManager` |
| `display.yaml` | Configuração de display/UI |
| `search.yaml` | Configuração de busca |
| `settings.json` | Settings serializadas |

## Variáveis de ambiente

> Carregadas em `deile.py` via `python-dotenv` se houver `.env` na raiz.

| Variável | Uso |
|---|---|
| `ANTHROPIC_API_KEY` | Habilita provider Anthropic |
| `OPENAI_API_KEY` | Habilita provider OpenAI |
| `DEEPSEEK_API_KEY` | Habilita provider DeepSeek |
| `GOOGLE_API_KEY` | Habilita provider Gemini |

> Pelo menos uma deve estar definida para a CLI iniciar. Caso contrário, mensagem de erro listando todas as opções e saída sem subir o agente.

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
