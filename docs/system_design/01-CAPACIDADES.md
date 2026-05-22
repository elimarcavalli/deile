# 01 — Capacidades operacionais do DEILE

> Este documento descreve **o que DEILE faz** em termos funcionais. Catalogações ficam em [`00-VISAO-GERAL.md`](00-VISAO-GERAL.md).

## Modos de execução da CLI

> Implementados em `deile.py`. Em ambos os modos, a CLI também aceita a mensagem via stdin quando os argumentos posicionais estão ausentes e stdin não é um TTY.

| Modo | Acionamento | Sessão usada | Implementação | Observação |
|---|---|---|---|---|
| Interativo (REPL) | `python3 deile.py` | `default_cli_session` (persistente durante a execução) | `DeileAgentCLI.run_interactive` | Input com autocompletion híbrida e renderização rica |
| One-shot | `python3 deile.py [--model PROVIDER:MODEL_ID] <mensagem>` | `oneshot_cli_session` | `_run_oneshot` | `response.content` em stdout; diagnósticos em stderr; código de saída 0 ou 1 |

## Capacidades funcionais (verificadas no código)

### Análise de intenção

| Capacidade | Componente |
|---|---|
| Detecção dirigida por padrões | `deile/core/intent_analyzer.py` |
| Catálogo de padrões | [`deile/config/intent_patterns.yaml`](../../deile/config/intent_patterns.yaml) com pesos, regex, keywords, requisitos de workflow e thresholds adaptativos |
| Métricas de performance | `deile/core/intent_metrics.py` |
| Mapeamento intent → tier | `deile/core/intent_tier_mapper.py` (`classify_tier`) |
| Cache de resultados | Embutido no `IntentAnalyzer` |

### Orquestração e planos

| Capacidade | Componente |
|---|---|
| Geração de plano com decomposição automática | `deile/orchestration/plan_manager.py` |
| Execução de workflow com rollback | `deile/orchestration/workflow_executor.py` |
| Gestão de tarefas (com persistência SQLite) | `deile/orchestration/task_manager.py`, `sqlite_task_manager.py` |
| Sistema de aprovação por nível de risco | `deile/orchestration/approval_system.py` — ver [`08-SEGURANCA.md`](08-SEGURANCA.md) |
| Gestão de artefatos e runs | `artifact_manager.py`, `run_manager.py` |

### Tools, comandos, parsers e personas

| Aspecto | Local / Detalhe |
|---|---|
| Taxonomia funcional de tools | [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md) |
| Auto-discovery de tools | `ToolRegistry.auto_discover()` para conjunto-padrão; demais via `register_tool()` |
| Categorias declaradas | `ToolCategory` em `deile/tools/base.py` (file, execution, search, system, analysis, network, database, other) |
| Slash commands | Processados por `deile/parsers/command_parser.py`, despachados pelo `CommandRegistry`; conjunto exato em `deile/commands/builtin/*.py` |
| Skills definidas pelo usuário | Arquivos `.md` em `~/.deile/skills/` (usuário) e `.deile/skills/` (projeto) são carregados na inicialização como slash commands; project skills têm prioridade sobre user skills em conflito de nomes. Implementado em `deile/commands/skill_loader.py`. Formato: frontmatter YAML opcional (`name`, `description`) + corpo em Markdown que será enviado ao LLM como prompt ao invocar o comando. O diretório `~/.deile/skills/` é criado automaticamente se ausente. |
| Pipeline de parsing | Em ordem de prioridade: comandos slash, referências a arquivos, diffs (`deile/parsers/`) |
| Personas | Markdown + YAML; hot-reload das instruções via `PersonaManager` quando habilitado |

### Memória, integrações com modelos e segurança

| Capacidade | Detalhamento |
|---|---|
| Memória em quatro camadas | [`06-MEMORIA.md`](06-MEMORIA.md). Ponto de entrada: `MemoryManager.store_interaction(...)` e acesso por camada |
| Multi-provider com seleção por tier | [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md) — Anthropic, OpenAI, DeepSeek, Gemini |
| Streaming default na CLI interativa | Controlado por `Settings.streaming_enabled` |
| Permissões, audit, scanner de segredos | [`08-SEGURANCA.md`](08-SEGURANCA.md) |

### Mensageria proativa (deile → bot)

DEILE pode **falar ativamente** em canais de mensageria através do daemon `deilebot` (repo separado: `elimarcavalli/deilebot`). O fluxo é o inverso do tradicional `bot → agent`: aqui o agente decide enviar a mensagem, e o daemon executa contra o provedor (Discord, hoje).

| Capacidade | Componente |
|---|---|
| Família de tools `messaging.discord_*` | `deile/tools/messaging/` — 7 operações (send_message, send_dm, react, start_thread, pin_message, mention_role, get_user_profile) |
| Adapter HTTP para o daemon | `deile/integrations/bot/` — wrapper sobre o cliente publicável `deilebot` |
| Configuração via env | `DEILE_BOT_ENDPOINT` e `DEILE_BOT_AUTH_TOKEN` (ver `.env.example`) |
| Auto-discovery condicional | Tools só aparecem quando o cliente está instalado E o endpoint está configurado |
| Aprovação para alto risco | `discord_send_dm` e `discord_mention_role` exigem `ApprovalSystem` antes de executar |
| Audit obrigatório | Cada chamada emite `AuditEvent(TOOL_EXECUTION)`; texto cru nunca é logado (apenas hash SHA8) |
| Categoria | `ToolCategory.MESSAGING` |

### Pipeline autônomo de issues/PRs/menções

> Detalhe em [`docs/2026-05-06_PIPELINE-AUTONOMO.md`](../2026-05-06_PIPELINE-AUTONOMO.md) e Decisões #18–#20, #30–#33 em [`DECISOES.md`](DECISOES.md).

| Capacidade | Detalhamento |
|---|---|
| Loop autônomo sobre o GitHub | `PipelineMonitor` (`deile/orchestration/pipeline/`) faz polling e dirige os estágios `classify` → `review` → `implement` (+`resume`) → `pr_review` → `follow_ups` por labels (`~workflow:*`, `~review:*`) |
| Execução plugável (Claude **ou** DEILE-worker) | `PipelineImplementer` selecionado por `dispatch_mode`: `claude -p` em worktree, ou despacho HTTP ao Pod `deile-worker` (loop 100% DEILE-a-DEILE) — Decisão #31 |
| Resume de trabalho parcial | Trabalho que para no meio é retomado sem `reset --hard`, com teto de tentativas/orçamento; `~workflow:bloqueada` exclui do auto-resume — Decisão #30 |
| Roteamento de menção/atribuição por papel | `process_mentions`: assignee/body em issue → entra no pipeline; assignee em PR → revisa+mergeia; reviewer-só → revisa e devolve ao autor (não mergeia); comment → atende ao pedido. Idempotência por `~mention:processado` — Decisão #32 |
| Quality-gate de PR | Review/merge sob a persona `reviewer` (`personas/instructions/reviewer.md`): avalia SOLID/SRP/DRY/KISS/segurança/idempotência/packaging + resolve threads, não só "testes verdes" — Decisão #32 |
| Cron genérico de prompts | `CronStore`/`CronRunner` (SQLite) dispara prompts naturais agendados como novos turns — intent #86 |

### Observabilidade

| Capacidade | Componente |
|---|---|
| Logger central | `deile/storage/logs.py` |
| Logger de debug | `deile/storage/debug_logger.py` |
| Repositório de uso e custo (SQLite) | `deile/storage/usage_repository.py` — base para budget e cost tracking |
| Event bus | `deile/events/event_bus.py` |

## Restrições conhecidas

| Restrição | Origem |
|---|---|
| Pelo menos uma chave de API de LLM é obrigatória no startup | `deile.py` (em `DeileAgentCLI.initialize` e `_run_oneshot`); ver [`09-CONFIGURACAO.md`](09-CONFIGURACAO.md) |
| `PluginSandbox` é skeleton — não isola plugins, e `PluginManager` nem o invoca | `deile/plugins/sandbox.py` (`PluginSandbox`); ver issue #54 |
| Auto-discovery de tools cobre um subconjunto fixo dos módulos | `auto_discover()` em `deile/tools/registry.py`; demais tools precisam de registro explícito |
| Coverage mínimo do `pytest` | `--cov-fail-under=80` em `pytest.ini` |

## Fluxos principais

| Tópico | Onde |
|---|---|
| Diagramas de sequência consolidados | [`10-DIAGRAMAS.md`](10-DIAGRAMAS.md) |
| Descrição em prosa do loop principal | [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md) |
