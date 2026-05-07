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
| `Settings.apply_overrides(d)` | Aplica dict aninhado (formato `.deile/settings.json`) sobre os campos planos |

> Regra: **nunca instanciar `Settings()` diretamente**. Sempre via `get_settings()`.

### Camadas (issue #111)

`get_settings()` lê preferências em hierarquia:

```
1. <projeto>/.deile/settings.json   (override de projeto)
2. ~/.deile/settings.json           (preferência do usuário)
3. Defaults da Settings dataclass   (fallback embutido)
```

A camada de projeto deep-merge sobre a camada do usuário (project wins em conflitos; chaves não-conflitantes coexistem). O legado `config/settings.json` continua sendo aceito como fallback **apenas** quando nenhum dos dois arquivos `.deile/settings.json` existir, com aviso de depreciação no log.

### Schema do `.deile/settings.json`

JSON aninhado por área. Apenas as chaves listadas em `_OVERRIDE_HANDLERS` são aplicadas; chaves desconhecidas são ignoradas (forward-compat). API keys NUNCA são lidas/escritas neste arquivo — secrets continuam em `.env`. Exemplo mínimo:

```json
{
  "logging":     { "level": "INFO", "to_file": true, "max_size_mb": 10, "backup_count": 5 },
  "ui":          { "streaming_enabled": true, "show_tool_details": false },
  "model":       { "default_provider": "anthropic", "max_context_tokens": 8000 },
  "caching":     { "enabled": true, "ttl_seconds": 3600 },
  "concurrency": { "max_concurrent_requests": 10, "request_timeout": 120 },
  "file_safety": { "enabled": true, "max_file_size_bytes": 1048576 },
  "deile_md":    { "enabled": true, "max_bytes": 65536 },
  "skills":      { "paths": [] },
  "environment": "development",
  "debug":       false
}
```

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

> **Status (issue #111):** este diretório foi limpo. As preferências antes
> ali agora vivem em `.deile/settings.json` (ver §Camadas). Apenas
> `config/deile_bot.yaml` permanece tracked (operacional do bot).

`config/settings.json` continua reconhecido como **fallback de leitura**
quando nenhum `.deile/settings.json` existe — emite aviso de depreciação
e não é regravado.

## Variáveis de ambiente

> Carregadas em `deile.py` via `python-dotenv` se houver `.env` na raiz.

| Variável | Uso |
|---|---|
| `ANTHROPIC_API_KEY` | Habilita provider Anthropic |
| `OPENAI_API_KEY` | Habilita provider OpenAI |
| `DEEPSEEK_API_KEY` | Habilita provider DeepSeek |
| `GOOGLE_API_KEY` | Habilita provider Gemini |
| `DEILE_BOT_ENDPOINT` | URL do daemon `deilebot` (control-plane HTTP). Sem isto, tools `messaging.discord_*` não registram |
| `DEILE_BOT_AUTH_TOKEN` | Bearer token do control-plane do daemon. Mesmo valor configurado nos dois lados |
| `DEILE_BOT_TIMEOUT_S` | Timeout (segundos) das chamadas do client. Default `10` |
| `DEILE_BOT_DEFAULT_GUILD_ID` | Guild Discord default (informativo, opcional) |

> Pelo menos uma das chaves de provider LLM deve estar definida para a CLI iniciar. Caso contrário, mensagem de erro listando todas as opções e saída sem subir o agente.

> As variáveis `DEILE_BOT_*` são opcionais: ausentes, a integração com o daemon fica desligada e as tools `messaging.discord_*` não aparecem na descoberta automática (sem warnings).

### Extra opcional `bot`

Para habilitar a mensageria proativa, instale o cliente:

```bash
pip install deile[bot]              # instala deilebot (apenas httpx + pydantic)
```

O daemon em si vive em `elimarcavalli/deilebot` e tem extras próprios (`discord`, `telegram`, etc.). Ver `deilebot/pyproject.toml`.

### Pipeline + Cron — variáveis de ambiente

> Todas opcionais. Ausentes, o pipeline e o cron simplesmente não iniciam automaticamente.

| Variável | Uso | Default |
|---|---|---|
| `DEILE_PIPELINE_REPO` | Repositório GitHub alvo no formato `owner/repo` | `elimarcavalli/deile` |
| `DEILE_PIPELINE_BASE_PATH` | Caminho absoluto da raiz do repositório onde `.worktrees/` será criado | Detectado automaticamente (busca ancestral com `.git` + `deile.py`) |
| `DEILE_PIPELINE_NOTIFY_USER_ID` | Discord snowflake para DMs de notificação de transições de estado | nenhum |
| `DEILE_PIPELINE_MONITOR_ID` | Identificador único deste monitor (1-32 chars `[a-zA-Z0-9_-]`); aparece em branch names, labels e worktree paths | `default` |
| `DEILE_PIPELINE_SHARD_INDEX` | Índice do shard neste monitor (int, `[0, SHARD_COUNT)`) | `0` |
| `DEILE_PIPELINE_SHARD_COUNT` | Total de shards no deploy (int `>= 1`); define quantas issues/PRs cada monitor atende por hash | `1` |
| `DEILE_PIPELINE_AUTOSTART` | Se `1`, o daemon `deilebot` inicia o `PipelineMonitor` automaticamente no boot | não setado |
| `DEILE_CRON_AUTOSTART` | Se `1`, o daemon `deilebot` inicia o `CronRunner` automaticamente no boot | não setado |
| `DEILE_CRON_DB_PATH` | Caminho absoluto do SQLite do `CronStore` | `<DEILE_PIPELINE_BASE_PATH>/data/cron.db` ou `<cwd>/data/cron.db` |

> O `pipeline_tool.py` e o `pipeline_command.py` leem essas variáveis diretamente via `os.environ` (pois são componentes de borda — não domínio); isso está alinhado com a regra "adapters podem ler env, core não pode".

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
