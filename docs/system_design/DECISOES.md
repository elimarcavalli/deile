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
| Decisão | Toda operação de I/O (arquivo, rede, processo, banco de dados) é implementada como `async def`. Sem `requests`, `time.sleep`, `open()` síncrono, nem driver de DB síncrono dentro de `async def`. Concorrência via `asyncio.gather()`. Cleanup via `async with`. Tools síncronas usam `SyncTool` que envolve em `asyncio.to_thread` |
| Evidência | `deile/tools/base.py:SyncTool`, uso de `aiohttp` e `aiosqlite`, `asyncio.gather` em `deile/orchestration/` |
| Motivação | Throughput: o agente processa múltiplos tools e LLM calls em paralelo; I/O síncrona bloquearia o loop inteiro |

---

## Decisão #5 — Arquitetura hexagonal (core ↔ adapters em `infrastructure/`)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 03-Princípios |
| Decisão | `deile/core/`, `deile/orchestration/`, `deile/memory/` não importam SDKs externos diretamente. Adapters externos vivem em `deile/infrastructure/` ou em providers concretos em `deile/core/models/`. Pydantic v2 para contratos |
| Evidência | `deile/infrastructure/` (providers de modelos), `deile/core/models/` (adaptação de APIs LLM) |
| Motivação | Trocar provider de LLM (Anthropic → OpenAI) sem tocar no núcleo |

---

## Decisão #6 — Memória em quatro camadas (working/episodic/semantic/procedural)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 06-Memória |
| Decisão | `MemoryManager` orquestra quatro camadas independentes: Working (TTL transitório), Episodic (eventos da sessão), Semantic (fatos persistentes), Procedural (padrões aprendidos). Cada camada tem módulo dedicado, interface async, e armazenamento separado |
| Evidência | `deile/memory/` (quatro módulos de camada + `memory_manager.py`) |
| Motivação | Diferentes TTLs, diferentes backends (in-memory, SQLite), diferentes queries — unificar numa única estrutura criaria acoplamento incorreto |

---

## Decisão #7 — Multi-provider com `ModelRouter` legado e `TierRouter` por tiers

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | `bootstrap_providers()` registra providers conforme chaves disponíveis. `TierRouter` despacha por tier (fast/balanced/powerful) em vez de modelo nominal. `ModelRouter` legado coexiste para compatibilidade. Providers concretos: Anthropic, OpenAI, DeepSeek, Google (legado direto) |
| Evidência | `deile/core/models/bootstrap.py`, `deile/core/models/tier_router.py`, `deile/core/models/model_router.py` |
| Motivação | Abstração de tier permite rotear para o melhor modelo disponível sem hardcode de nome |

---

## Decisão #8 — Circuit breaker por provider e budget por sessão/diário/mensal

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | Cada provider tem um `CircuitBreaker` (estados: closed/open/half-open, threshold de falhas configurável). `BudgetGuard` rastreia custo em três janelas (sessão, diário, mensal) e aborta chamadas que excederiam o limite |
| Evidência | `deile/core/models/circuit_breaker.py`, `deile/core/models/budget_guard.py`, `UsageRepository` |
| Motivação | Falha isolada de um provider não derruba o agente; overspend acidental é bloqueado antes da chamada |

---

## Decisão #9 — Sistema de permissões baseado em regras + audit logging tipado

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `PermissionManager` avalia regras em `config/permissions.yaml` (resource, action, level). `AuditLogger` registra `AuditEvent` tipado (nunca formato livre) em arquivo rotacionado. Toda ação privilegiada passa por `check_permission()` antes de executar |
| Evidência | `deile/security/permissions.py`, `deile/security/audit.py`, `config/permissions.yaml` |
| Motivação | Auditabilidade de operações sensíveis; revogação de acesso sem mudança de código |

---

## Decisão #10 — Sistema de aprovação por nível de risco em planos

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `ApprovalSystem` classifica ações por risco (low/medium/high/critical). Ações acima do threshold configurável bloqueiam execução até aprovação explícita do operador. `PlanManager` submete planos ao sistema antes de executar |
| Evidência | `deile/security/approval.py`, `deile/orchestration/plan_manager.py` |
| Motivação | Agente autônomo com bash e file tools pode causar danos irreversíveis; aprovação por risco é o único gate antes da execução |

---

## Decisão #11 — `Settings` como singleton via `get_settings()`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 09-Configuração |
| Decisão | `deile/config/settings.py` expõe apenas `get_settings()`. O dataclass `Settings` não é instanciado diretamente. `ConfigManager` carrega YAML/JSON e merge com env vars. Leitura de `os.environ` é proibida no código de domínio |
| Evidência | `deile/config/settings.py:get_settings`, `deile/config/manager.py:ConfigManager` |
| Motivação | Único ponto de override para testes; consistência entre módulos que leem a mesma chave |

---

## Decisão #12 — Personas instanciadas por instruções em Markdown + YAML de capacidades

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes |
| Decisão | Cada persona tem um arquivo `.md` em `deile/personas/instructions/` (prosa de instruções) e um `.yaml` em `deile/personas/library/` (capacidades, tools, preferências). `PersonaManager` carrega e compõe ambos. Mudança de comportamento = editar Markdown, sem Python |
| Evidência | `deile/personas/manager.py`, `deile/personas/instructions/*.md`, `deile/personas/library/*.yaml` |
| Motivação | Non-engineers podem ajustar personas; persona = dados, não código |

---

## Decisão #13 — Hot-reload de configuração e plugins via `watchdog`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 09-Configuração |
| Decisão | `ConfigManager` usa `watchdog` para observar mudanças em `config/`. Plugins são carregados por `PluginManager` com `hot_loader`. Mudança de arquivo dispara re-load sem restart do processo |
| Evidência | `deile/config/manager.py` (watcher), `deile/plugins/manager.py`, `deile/plugins/hot_loader.py` |
| Motivação | Iteração rápida em desenvolvimento; mudança de configuração em produção sem downtime |

---

## Decisão #14 — Persistência (memória episódica/semântica/uso) em SQLite

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 06-Memória, 07-Integrações LLM |
| Decisão | Episodic memory, semantic memory e usage tracking persistem em SQLite via `aiosqlite`. Cada módulo gerencia seu próprio schema e migrations. Working memory e procedural memory ficam in-memory (TTL-based) |
| Evidência | `deile/memory/episodic_memory.py`, `deile/memory/semantic_memory.py`, `deile/storage/usage_repository.py` |
| Motivação | SQLite: zero infra, ACID, portátil. Async via aiosqlite não bloqueia o loop. Separação de schemas evita lock contention entre módulos |

---

## Decisão #15 — Streaming-first: `process_input_stream` é o caminho default da CLI

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 05-Fluxo |
| Decisão | `DeileAgent.process_input_stream(user_input)` é o método principal, retornando um `AsyncIterator` de tokens/events. `process_input` (não-stream) é wrapper. A CLI consome o stream e imprime progressivamente |
| Evidência | `deile/core/agent.py:process_input_stream`, `deile/cli.py` (consumo do stream) |
| Motivação | UX: usuário vê resposta começar a aparecer imediatamente, mesmo para respostas longas ou tool chains |

---

## Decisão #16 — Two-flag flag de fallback `use_legacy_gemini_only` em `model_providers.yaml`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 07-Integrações LLM |
| Decisão | Se `use_legacy_gemini_only: true` em `model_providers.yaml`, `bootstrap_providers()` usa `_bootstrap_legacy_gemini()` em vez do novo `TierRouter`. Isso mantém compatibilidade com deployments que ainda não migraram para o novo router |
| Evidência | `deile.py:_use_legacy_gemini_only`, `deile/core/models/bootstrap.py:_bootstrap_legacy_gemini` |
| Motivação | Migração incremental sem romper usuários existentes |

---

## Decisão #17 — Separação `deile`/`deilebot` + protocolo HTTP local (Bearer, 127.0.0.1) para a flecha reversa `agente → bot`

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 04-Componentes, 08-Segurança |
| Decisão | `deilebot` é repo separado (`elimarcavalli/deilebot`). O agente DEILE chama o bot via HTTP local (`http://127.0.0.1:<port>/v1/...`) com Bearer token. Tools `messaging.*` são registradas apenas quando `import deilebot` bem-sucede e `DEILE_BOT_ENDPOINT` + `DEILE_BOT_AUTH_TOKEN` estão presentes. O bot expõe `/v1/send`, `/v1/react`, `/v1/dm`, `/v1/thread`, `/v1/pin`, `/v1/mention-role`, `/v1/user-profile`, `/v1/health` |
| Evidência | `deile/integrations/bot/client.py`, `deile/integrations/bot/config.py`, `deile/tools/messaging/` |
| Motivação | (1) Isolamento de processo: o bot (discord.py, gateway WS) é longa-vida e stateful — runs no mesmo processo que o agente criaria coupling e tornaria testes impossíveis. (2) Segurança: o token Discord fica no processo do bot, nunca no agente. |

---

## Decisão #18 — Hash sharding para execução paralela de monitores

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 03-Princípios, 02-Arquitetura |
| Decisão | `MonitorIdentity` carrega `shard_index` e `shard_count`. Cada issue/PR passa por `compute_batch_id_for_number(kind, number)` → SHA-256 → `int(hex, 16) % shard_count`. O monitor processa apenas itens cujo shard bate com seu `shard_index`. Permite N instâncias paralelas sem coordenação explícita |
| Evidência | `deile/orchestration/pipeline/monitor.py:MonitorIdentity`, `compute_batch_id_for_number` (ver decisão #23) |
| Motivação | Escalar horizontalmente o pipeline sem shared state; cada shard é idempotente |

---

## Decisão #19 — Cron genérico separado do scheduler do pipeline

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes |
| Decisão | `CronStore` (SQLite) + `CronRunner` gerenciam jobs cron genéricos (qualquer callable registrado). `ScheduleStore` (YAML) + lógica em `tick()` gerenciam stages do pipeline DEILE. Os dois sistemas coexistem sem acoplamento — `CronRunner` não sabe nada de issues/PRs |
| Evidência | `deile/storage/cron_store.py`, `deile/orchestration/cron_runner.py` vs `deile/orchestration/pipeline/schedule_store.py`, `monitor.py:tick()` |
| Motivação | Cron genérico pode executar qualquer tarefa (backup, cleanup, notificação); acoplar isso ao pipeline criaria dependência circular |

---

## Decisão #20 — Strip de `ANTHROPIC_API_KEY` no subprocess do Claude Code

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | `ClaudeDispatcher` (em `deile/tools/`) remove `ANTHROPIC_API_KEY` do ambiente antes de invocar `claude` CLI como subprocess (`prefer_subscription_auth=True`). Isso força o Claude Code a usar autenticação por subscription (OAuth) em vez do API key do operador |
| Evidência | `deile/tools/claude_dispatcher.py:prefer_subscription_auth` |
| Motivação | Evitar que o subprocess herde e potencialmente vaze a chave do operador; subscription auth não expõe credencial no processo filho |

---

## Decisão #21 — Schedule padrão completo + fallback legacy para stages ausentes

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

## Decisão #24 — TOCTOU mitigation em `claim_with_batch`: re-fetch após `add_labels`

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 03-Princípios (Security-First), 02-Arquitetura |
| Decisão | Após `add_labels(label)` em `claim_with_batch`, o cliente faz um re-fetch da issue/PR e verifica se há labels de batch de outros monitores além do próprio. Se detectado, remove o label próprio e retorna `None` (falha silenciosa). Isso mitiga a race condition TOCTOU onde dois monitores adicionam labels quase simultaneamente (GitHub API não é transacional). |
| Evidência | `deile/orchestration/pipeline/github_client.py:claim_with_batch` |
| Motivação | O GitHub REST API não oferece operação atômica "adicionar label apenas se ausente". A janela entre `get_issue` (check) e `add_labels` (use) é explorável. O re-fetch post-add detecta o conflito após o fato e recua, garantindo que apenas um monitor processe o item. O recuo é best-effort (remoção pode falhar em caso de erro de rede). |

---

## Decisão #25 — Comandos slash declaram CLI flags via metadata; argparse é gerado pelo registry

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes, 02-Arquitetura |
| Decisão | Cada subclasse de `SlashCommand` declara atributos opcionais (`cli_flag`, `cli_extra_flags`, `cli_takes_arg`, `cli_arg_metavar`, `cli_help`, `cli_requires_provider`). Em runtime, `deile/commands/cli_flags.py:build_cli_flag_specs(registry)` percorre o registry e produz uma lista de `CLIFlagSpec`; `add_command_flags_to_parser(parser, specs)` injeta cada spec como um argumento argparse. `deile/cli.py` apenas descobre e despacha — nenhuma flag é hardcoded ali. Adicionar nova flag é mudança de metadata, sem editar `cli.py`. |
| Evidência | `deile/commands/base.py:SlashCommand` (atributos `cli_*`); `deile/commands/cli_flags.py:CLIFlagSpec/build_cli_flag_specs/add_command_flags_to_parser`; `deile/cli.py:main()` (linhas que chamam `build_cli_flag_specs`); `deile/tests/cli/test_cli_flags.py` (smoke + estrutural) |
| Motivação | (1) Alinhar com Registry Pattern (decisão #3 / princípio 3 em `03-PRINCIPIOS-ARQUITETURAIS.md`): comandos são plugáveis, descobríveis pelo registry; o CLI deve consumir o registry, não duplicar a lista. (2) A issue #126 listou 19 flags faltantes mais um padrão para expansão futura — manualmente listá-las em `cli.py` exigiria sincronização permanente. (3) Flags que não exigem provider de LLM (`cli_requires_provider=False`, default) bypassam `bootstrap_providers()` e funcionam sem API key, preservando UX de diagnóstico (`--version`, `--status`, `--tools`, etc.). |

---

## Decisão #26 — Project layer de `.deile/settings.json` exige opt-in explícito por diretório (allowlist)

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 08-Segurança, 09-Configuração |
| Decisão | `_load_layered_settings` em `deile/config/settings.py` deixa de aplicar `<cwd>/.deile/settings.json` incondicionalmente. O usuário declara em `~/.deile/settings.json` a chave `trust.project_layer_dirs: ["<abs-path>", ...]` listando os diretórios cujo project layer ele confia. Diretórios fora da allowlist são tratados conforme `trust.project_layer_default`: `"auto"` (default — aplica com warning ruidoso, grace-period de uma versão minor) ou `"deny"` (ignora silenciosamente após um warning). Adicionalmente, `set_setting`/`add_skills_path`/`remove_skills_path` em `deile/commands/settings_manager.py` agora exigem `PermissionManager.check_permission(resource="settings:<scope>:<detail>", action="write")` antes da escrita e emitem `AuditEvent(SECURITY_POLICY_CHANGED)` no resultado. Valores são fingerprinted via SHA-256 truncado; chaves que casam com `_SECRET_KEY_PATTERNS` viram `"<redacted>"`. `Settings.load_from_file` (caminho legado) filtra `config_dict` por allowlist explícita das chaves do `_OVERRIDE_HANDLERS`. |
| Evidência | `deile/config/settings.py:_load_layered_settings`, `_is_project_layer_trusted`, `_OVERRIDE_HANDLERS` (chaves `trust.project_layer_dirs`, `trust.project_layer_default`); `deile/commands/settings_manager.py:set_setting`, `add_skills_path`, `remove_skills_path`, `_emit_settings_audit`, `_value_fingerprint`, `_validate_against_override_handlers`; `deile/security/permissions.py:_load_default_rules` (regra `settings_write_default`); `deile/tests/test_settings_manager_audit.py`, `deile/tests/test_settings_layered_trust.py` |
| Motivação | (1) **Trust-boundary**: um repo de terceiro pode commitar `.deile/settings.json` desligando `file_safety`, ativando `allow_all_file_types`, ou redirecionando `working_directory` — o usuário que clona e roda `python deile.py` perde proteções sem confirmar nada. Igual ao caso de `.deile/skills/` (Pilar 08 §"Skills como fronteira de confiança"), o project layer agora exige opt-in explícito. (2) **Permissão antes da ação** (Pilar 03 §5): mutar `enable_file_safety_checks`, `caching.enabled`, `debug` é mudança de postura de segurança e deve passar pelo gate. (3) **Audit tipado** (Pilar 03 §5): toda escrita em settings é audit-logged via tipo `SECURITY_POLICY_CHANGED` (já existia no enum, ninguém emitia). Valores brutos não vão para o log — só hash + flag de redação para chaves potencialmente sensíveis. (4) **Defesa em profundidade no caminho legado**: `load_from_file` aceitava `cls(**config_dict)` com qualquer chave do dataclass, expondo `working_directory='/etc'` como vetor. A allowlist espelha o `_OVERRIDE_HANDLERS` (canonical-safe set). |
| Alternativas consideradas | (a) Confirmação interativa via `ApprovalSystem` ao detectar project layer não-confiável: rejeitada por quebrar fluxos automatizados (CI). (b) Usar `_OVERRIDE_HANDLERS` como permissão estática (sem `PermissionManager`): rejeitada por colidir com a regra "Permissão antes da ação" — o gate é runtime-configurável via `config/permissions.yaml`. (c) Migração imediata para `'deny'` por default: rejeitada por quebrar CIs/pipelines em uso hoje sem sinal de transição; o knob `'auto'` dá uma versão de aviso antes do flip. (d) Implementar como `pydantic.BaseSettings`: descartada — fora do escopo desta issue e exigiria migração mecânica de todo o dataclass `Settings` mais a remoção do mapeamento manual `_OVERRIDE_HANDLERS` / `_JSON_FIELD_MAP`; reaproveita zero do código atual e não traz benefício de segurança aqui. |
| Histórico | **2026-05-08 (patch — review feedback PR #135)**: 1) **Fail-closed por default**: a regra `settings_write_default` em `permissions.py:_load_default_rules` passou de `PermissionLevel.WRITE` (allow) para `PermissionLevel.READ` (deny). Operadores precisam adicionar uma regra `settings_write_interactive` em `config/permissions.yaml` para habilitar escritas — alinhado com Pilar 03 §5 ("Permissão antes da ação"). 2) **`set_preference` agora passa pelo mesmo pipeline** (gate + audit + secret-key check) — antes era um endpoint público sem proteção. 3) **`_set_typed` passou a recusar não-listas em campos de lista** (`_LIST_ATTRS`) — antes, `trust_project_layer_dirs: "/single"` virava string e `_is_project_layer_trusted` iterava por caractere. 4) **`Settings.load_from_file` aplica converters** dos `_OVERRIDE_HANDLERS` (não só filtra nomes), prevenindo `enable_file_safety_checks: "yes-please"` de colar no atributo bool. 5) **`_emit_settings_audit` passou a ser chamado em refusal de chave-segredo** — antes só validation_failed e permission_denied emitiam audit. 6) **Logger de validation_failed não vaza mais o value cru** — usa `_value_fingerprint` e mensagem do conversor é sanitizada. 7) **Comparação de paths case-insensitive** via `os.path.normcase` (HFS+/APFS/NTFS). 8) **Tests root conftest** isola `AuditLogger` por sessão para não poluir `~/.deile/logs/security_audit.log`. 9) **Helpers de segurança extraídos** para `deile/commands/_settings_security_hooks.py`. 10) **`/skills add` e `/skills remove` distinguem denial de no-op** via método `*_detailed` retornando `(success, reason)`. |

---

## Decisão #27 — Stack de containerização em K8s para isolar deile-Job/bot/deile-shell do host

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 14-Containerização, 08-Segurança |
| Decisão | Todos os workloads de produção rodam em pods K8s (Rancher Desktop / k3s) dentro do namespace `deile`, com isolamento multi-camada: (1) Segredos entregues como arquivos em `/run/secrets/<role>/` montados via volume `secret` (mode 0440) — nunca como variáveis de ambiente no spec do pod; `wrapper.py` lê os arquivos, injeta em `os.environ`, chama `bootstrap_providers()` e depois chama `_pop_sensitive_keys()` para remover as chaves LLM de `os.environ` (DEILE_BOT_DISCORD_TOKEN e DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN são mantidos pelo discord.py em runtime, compensados pela tool whitelist). (2) PSS `restricted` aplicado via labels do namespace (`enforce`/`audit`/`warn`, pinados em `v1.29`). (3) Cada container: `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem: true`, `capabilities.drop: ["ALL"]`, `seccompProfile: RuntimeDefault`, `runAsNonRoot: true`, UID/GID 10001. (4) NetworkPolicy default-deny; egress/ingress abertos apenas para DNS (UDP/TCP 53), LLM HTTPS (443, blocos RFC1918 excluídos), bot control-plane (8765 entre role=deile e app=deilebot). (5) `automountServiceAccountToken: false`, `enableServiceLinks: false`. (6) `imagePullPolicy: Never` com `deile-stack:local` carregado via `nerdctl --namespace k8s.io build`. |
| Evidência | `infra/k8s/Dockerfile`; `infra/k8s/wrapper.py` (`_SENSITIVE_KEYS`, `_patch_deile_bootstrap`, `_pop_sensitive_keys`); `infra/k8s/manifests/00-namespace.yaml` (PSS labels); `infra/k8s/manifests/20-bot-deployment.yaml`, `infra/k8s/manifests/30-deile-job.yaml`, `infra/k8s/manifests/35-deile-interactive.yaml` (securityContext); `infra/k8s/manifests/40-network-policy.yaml` (5 policies); `infra/k8s/deploy.py` (orquestrador Python build/up/test/start/stop; `run.sh` é shim) |
| Motivação | (1) Segredos como env vars ficam visíveis em `/proc/<pid>/environ` para qualquer processo com permissão de leitura no mesmo host — o modelo file+pop reduz a janela de exposição aos milissegundos antes de `bootstrap_providers()`. (2) PSS restricted bloqueia vetores de escalada de privilégio na camada de admission do cluster sem exigir validação manual em cada spec. (3) NetworkPolicy default-deny reduz blast radius de um container comprometido: ele não pode alcançar a rede interna do cluster nem serviços externos além do necessário. (4) `readOnlyRootFilesystem` impede que malware persista no container filesystem. (5) `automountServiceAccountToken: false` elimina acesso não intencional à API K8s se o token vazar via path traversal. |

---

## Decisão #28 — Tool whitelist no bot embutido e default-`messaging` no deile-oneshot Job

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 14-Containerização, 04-Componentes |
| Decisão | O `wrapper.py` aplica restrições de toolset diferenciadas por role: (a) **Role `bot`**: `_install_tool_whitelist("bot")` patcha `DeileAgent.__init__` para desabilitar todas as tools que não estejam no whitelist derivado de `deile.tools.messaging` (`_messaging_tool_whitelist()`). O agente embutido do bot processa prompts de usuários Discord arbitrários — o toolset cheio (bash, file, execution) representa risco inaceitável. O whitelist é construído via `auto_discover()` + inspeção de `deile/tools/messaging/` para eliminar dependência de lista hardcoded. (b) **Role `deile`** (one-shot Job): `_install_tool_whitelist("deile")` aplica o mesmo whitelist `messaging`; o prompt do Job vem do campo `args` do Kubernetes Job spec, controlado pelo operador — mas o toolset é restringido para limitar o raio de ação no caso de injeção de prompt pela resposta do bot. (c) **Role `deile-shell`** (interativo): sem whitelist — o operador acessa via `kubectl exec` com autenticação K8s, equivalente ao processo local; o toolset completo é necessário para uso de desenvolvimento. A distinção de roles é determinada em tempo de execução pelo primeiro argumento posicional do `wrapper.py` (`deile` / `bot` / `deile-shell`). |
| Evidência | `infra/k8s/wrapper.py` (`_messaging_tool_whitelist`, `_install_tool_whitelist`, `_run_deile`, `_run_bot`); `infra/k8s/manifests/30-deile-job.yaml` (args: `wrapper.py deile`); `infra/k8s/manifests/20-bot-deployment.yaml` (args: `wrapper.py bot`); `infra/k8s/manifests/35-deile-interactive.yaml` (args: `wrapper.py deile-shell`) |
| Motivação | (1) **Untrusted input por design**: o bot recebe mensagens de qualquer usuário Discord na allowlist — injeção de prompt é o vetor de ataque mais provável; limitar o toolset ao conjunto `messaging` torna o agente útil sem expor operações destrutivas. (2) **Prompt fixo vs. toolset livre**: o Job deile-oneshot tem prompt fixo (spec do Job), mas a resposta do bot pode conter instruções secundárias; o whitelist é compensating control para esse caso. (3) **Defense in depth**: a whitelist é adicional ao NetworkPolicy (sem egress exceto LLM/bot) — ambos precisam ser violados para execução arbitrária de código com acesso externo. (4) Evitar lista hardcoded de nomes de tool (seria frágil a renomeações): derivar do módulo `deile.tools.messaging` via `auto_discover()` mantém a whitelist automaticamente sincronizada. |

---

## Decisão #29 — Permission gate + audit logging do `dispatch_deile_task` adiados para feature dedicada

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 08-Segurança |
| Decisão | A tool `dispatch_deile_task` (em `deile/tools/dispatch_deile_task.py`) atravessa input não confiável do Discord para execução remota privilegiada (toolset completo do DEILE em worker isolado), **mas não passa por `PermissionManager.check_permission()` nem emite `AuditEvent(TOOL_EXECUTION)`** hoje. Esta lacuna é **conhecida e adiada**: a PR atual (#233) é refator hexagonal puro do transporte (extração para `deile/infrastructure/deile_worker_client.py`); introduzir o gate exige (1) convenção nova de resource string (`dispatch:<channel_id>` ou similar — não existe padrão precedente para tools `bot → worker`), (2) atualização correspondente de `config/permissions.yaml` com regra default fail-closed + override interactive, (3) expansão do pilar 08 (seção "Mensageria proativa") para cobrir o novo gate e os campos de `details` do audit (SHA8(brief), channel_id, user_message_id, persona, task_id, error_code, três emissões: pending/success/failed). Cada um desses itens é decisão de design separada; agrupá-los nesta PR seria scope creep. Defense-in-depth provisória: o `wrapper.py` em `infra/k8s/` aplica tool whitelist `messaging` no role `bot` (decisão #28), impedindo que o bot embutido invoque tools privilegiadas além do conjunto `messaging.*` + `dispatch_deile_task` — qualquer abuso fica confinado ao próprio worker, que roda em pod isolado com NetworkPolicy default-deny (decisão #27). O cooldown anti-loop de 30s por `channel_id` adiciona uma terceira camada compensatória contra flooding. |
| Evidência | TODO inline em `deile/tools/dispatch_deile_task.py:execute()` (referencia esta decisão); `deile/tools/dispatch_deile_task.py` (sem chamada a `PermissionManager` ou `audit_logger`); contraste com `deile/tools/messaging/_base.py` (gate + audit no padrão `MessagingTool`); `infra/k8s/wrapper.py:_install_tool_whitelist` (decisão #28) |
| Motivação | (1) **Atomicidade do refator**: extrair o transporte para a fronteira hexagonal (decisão #5) é mudança puramente estrutural — adicionar gate + audit é mudança comportamental e de superfície de configuração; misturá-los obscurece o diff. (2) **Convenção de resource string**: nenhuma tool tipo "ponte para serviço remoto" tem precedente em `permissions.yaml`; escolher o formato exige discussão (channel_id como leaf? persona como component? brief hash como qualifier?). (3) **Cobertura compensatória atual**: tool whitelist (#28) + NetworkPolicy (#27) + cooldown de 30s reduzem a janela explorável; o ataque que o gate bloquearia (abusar do bot para spawnar workers para canais não autorizados) já está restrito ao perímetro do cluster. (4) **Rastreabilidade**: o TODO no código aponta para esta entrada — qualquer revisor futuro encontra o motivo do adiamento sem caçar PR/issue history. |
| Follow-up | Issue dedicada deve cobrir: (a) regra `dispatch_deile_task_default` em `config/permissions.yaml` com `resource_pattern: '^dispatch:.*$'` e `permission_level: read` (deny); (b) regra opt-in `dispatch_deile_task_allowed_channels` com `resource_pattern: '^dispatch:(<canal_id_1>\|<canal_id_2>)$'`; (c) helper `_resolve_permission_manager` análogo ao de `messaging/_base.py`; (d) três `log_tool_execution` (pending pré-cooldown, success com `task_id`, failed com `error_code`); (e) atualização do pilar 08 incluindo `dispatch_deile_task` na tabela "Mensageria proativa". |

---

## Decisão #30 — Resume de trabalho parcial no pipeline (in-place no PVC, ground-truth-first, com guarda de progresso e teto)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 05-Fluxo, 08-Segurança |
| Decisão | Quando o `deile-worker` para uma **implementação** ou um **review/merge** no meio (estourou o cap de tool-calls, timeout/crash/restart, ou o agente declarou `INCOMPLETO`), o pipeline **RETOMA** o trabalho reusando a branch + arquivos *untracked* no workspace persistente por canal — **sem** `git reset --hard` (o *fresh start* continua resetando). Mecânica: (a) **Briefs de resume** em `implementer.py` (`_WORKER_IMPLEMENT_RESUME_BRIEF`/`_WORKER_REVIEW_RESUME_BRIEF`) que não resetam e injetam journal + diff + leitura de untracked; (b) **Worker** (`worker_server.py` + módulo puro `infra/k8s/_worker_resume.py`) que, no caminho pipeline, escreve/auto-resume o journal `.deile-progress.md` (híbrido: agente escreve ao pausar, worker resume o transcript como fallback), persiste `.deile-progress.json` (`tentativa`/`fingerprint`/`budget_acumulado_s`) e devolve resultado estruturado `{ended, pr_url, motivo_bloqueio, motivo_fim_loop, fingerprint, tentativa, budget_acumulado_s}`; (c) **Detecção de fim ground-truth-first** (`detect_end_state`): decide CONCLUÍDO (PR confirmada; review exige merge), INCOMPLETO (sem PR) ou BLOQUEADO (só o agente declara `BLOQUEADO:`) pelo estado real, sem depender de formato do modelo; (d) **Guarda de progresso** (`fingerprint` substantivo — diff/untracked **ignorando** `.deile-progress.*` e meta) — fingerprint idêntico entre tentativas = 0 progresso; (e) **Stages** (`stages.py`): seleção `resume_in_progress_issues` (issues em `~workflow:em_implementacao`, continuáveis, **sem** `~workflow:bloqueada`) respeitando cadência + teto de tentativas + guarda, e resume também no stage de review/merge; (f) **Fluxo de bloqueio**: comentário do impedimento real na issue/PR + label `~workflow:bloqueada` + DM (notifier); `~workflow:bloqueada` exclui do auto-resume e do stage de implementação (humano remove para desbloquear). Estado pipeline-side (cadência, fingerprint anterior, tentativa/budget) vive no `ResumeTracker` (`resume_state.py`) anexado à instância do monitor — coordenação, não memória de agente. Os arquivos `.deile-progress.*` nunca entram no commit/PR (`.git/info/exclude` + un-stage defensivo). |
| Evidência | `deile/orchestration/pipeline/implementer.py` (briefs de resume, `_build_resume_block`, `_outcome_from_worker_response`, campos de resume em `WorkOutcome`); `infra/k8s/_worker_resume.py` (fingerprint, journal, `detect_end_state`, gitignore); `infra/k8s/worker_server.py` (`_compute_resume_result`, `_parse_resume_ctx`); `deile/orchestration/pipeline/stages.py` (`resume_in_progress_issues`, `_finalize_implement_outcome`, `_block_issue`, `_block_pr`); `deile/orchestration/pipeline/resume_state.py` (`ResumeTracker`); `deile/orchestration/pipeline/labels.py` (`WORKFLOW_BLOCKED`); `deile/orchestration/pipeline/notifier.py` (`implementation_resumed`, `implementation_blocked`); `deile/config/settings.py` (`pipeline_resume_*`) |
| Motivação | (1) **Tarefas grandes nunca concluíam**: cada tentativa recomeçava do zero (`reset --hard`), descartando trabalho parcial não commitado — tarefas que excedem um turno ficavam presas para sempre (evidência: issue #253, 42 rounds gastos sem implementar/commitar). (2) **Ground-truth-first** porque o modelo nem sempre obedece formato e pode crashar — decidir pelo estado git/PR real funciona até em crash; o único sinal vindo do agente é `BLOQUEADO:` (só ele sabe de impedimento). (3) **Guarda de progresso + teto** (tentativas + orçamento) evitam loop infinito gastando tokens quando o agente não avança. (4) **Bloqueio explícito** (label + comentário + DM) dá controle ao humano sem auto-retry-forever (que produziria storm de DMs — a regressão de #253). (5) **PVC-only** (sem backup remoto): se o PVC for destruído perde-se o parcial — trade-off aceito pelo operador. |
| Configuração | `pipeline_resume_enabled` (default `true`), `pipeline_resume_interval` (s, default `0` = imediato), `pipeline_resume_max_attempts` (default `10`), `pipeline_resume_budget` (s, default `0` = sem teto de tempo). Resolvidos em `build_default_pipeline_config`; resume só ativa no caminho `deile_worker` (o contrato estruturado vive lá). |
| Fora do escopo | Tratamento de **menções** (coberto depois pela Decisão #32); backup remoto do parcial (PVC-only); troca do modelo do worker. |

---

## Decisão #31 — `PipelineImplementer` como estratégia plugável (Claude `-p` vs deile-worker HTTP)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 04-Componentes |
| Decisão | O trabalho pesado dos estágios `implement`/`review`/`mention` é delegado a uma **estratégia** `PipelineImplementer`, não mais hardcoded em `claude -p`. Duas implementações: `ClaudeImplementer` (cria worktree local e roda `claude -p` — comportamento legado, preservado verbatim) e `WorkerImplementer` (POSTa um brief ao control-plane do `deile-worker` por HTTP; o worker clona, branca, implementa/revisa, testa e abre/mergeia a PR no seu próprio workspace isolado). A seleção é por `PipelineConfig.dispatch_mode` (`claude` \| `deile_worker`, com aliases); valor desconhecido cai em Claude com warning. Assim **o Claude vira uma opção, não dependência** — o loop autônomo pode rodar inteiramente DEILE-a-DEILE (deepseek/etc no worker). |
| Evidência | `deile/orchestration/pipeline/implementer.py` (`PipelineImplementer` ABC, `ClaudeImplementer`, `WorkerImplementer`, `build_implementer`); `deile/infrastructure/deile_worker_client.py` (`DeileWorkerClient`, `build_dispatch_payload`); `infra/k8s/worker_server.py` (control-plane do worker) — issue #255 |
| Motivação | (1) Não acoplar o pipeline autônomo à assinatura/CLI do Claude Code; (2) permitir um loop 100% DEILE (o worker roda outro DEILE com o modelo configurado); (3) isolar a execução pesada no Pod `deile-worker` (sandbox K8s) em vez de no host do monitor. |

---

## Decisão #32 — Roteamento de menção/atribuição por papel + persona `reviewer` como quality-gate

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 05-Fluxo, 04-Componentes, 08-Segurança |
| Decisão | `process_mentions` deixou de ser um despachante one-shot e virou um **roteador por papel**, injetando o trabalho nas esteiras existentes (ganhando idempotência, resume e convenção de branch sem máquina de estado paralela): **issue + assignee/menção-no-corpo** → aplica `~workflow:nova` (a esteira normal assume: review → implement com resume → PR em branch `auto/issue-N` → review pela persona reviewer); **PR + assignee** → `work_merge` (revisa, resolve threads criticamente, corrige e mergeia); **PR + reviewer-só** → `review_only` (revisa, posta review via REST, marca o **autor como assignee** e **NUNCA mergeia** — o merge é do dono); **PR + comment/body** → `address` (atende ao pedido + resolve threads, sem mergear); **comment em issue** → faz o que o comentário pede. Idempotência cross-tick por label sticky **`~mention:processado`** (gatilhos assignee/reviewer/body não re-disparam a cada tick; comment é regido por cursor). A review/merge de PR roda sob a persona **`reviewer`** (`personas/instructions/reviewer.md` + `library/reviewer.yaml` + `persona_config.yaml`; `"reviewer"` no `WorkerPersona`), um quality-gate de arquitetura (SOLID/SRP/DRY/KISS/segurança/idempotência/packaging), não só "testes verdes". Resume e teto de tentativas reaproveitam o `ResumeTracker`. |
| Evidência | `deile/orchestration/pipeline/stages.py` (`process_mentions`, `_route_issue_to_pipeline`, `_mark_mention_done`, `_STICKY_TRIGGER_TYPES`); `deile/orchestration/pipeline/implementer.py` (`mention(mode=...)`); `deile/orchestration/pipeline/briefs.py` (`_WORKER_REVIEW_ONLY_BRIEF`, `_WORKER_PR_ADDRESS_BRIEF`, review brief com resolução de threads); `deile/orchestration/pipeline/labels.py` (`MENTION_DONE`); `deile/orchestration/pipeline/github_client.py` (`MentionTrigger`, `list_issues_assigned_to`/`list_prs_assigned_to`/`list_prs_with_review_requests`/`search_items_mentioning` — todas via `gh api -X GET`); `deile/personas/instructions/reviewer.md` — issues #253/#261 |
| Motivação | (1) Atender as 4 formas de acionamento (assignee/reviewer/body/comment) de forma consistente; (2) eliminar o storm de DMs/dispatches duplicados (`~mention:processado` + dispatch síncrono que bloqueia o tick); (3) reviewer-só não deve mergear nem invadir — devolve ao autor; (4) o quality-gate precisa avaliar arquitetura, não só rodar testes; (5) as queries de menção falhavam em runtime (404/422) por usarem POST implícito do `gh api --field` — corrigidas com `-X GET`. |
| Fora do escopo | Resume do caminho de **comment** (ad-hoc, cursor-bounded); lock `~batch:` no caminho de menção (gap multi-monitor conhecido — réplica única não sofre). |

---

## Decisão #33 — Triagem de PR escopada a branch própria + lock `~batch:` só em multi-monitor

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 02-Arquitetura, 03-Princípios |
| Decisão | `classify_new_prs` só aplica `~review:pendente` a PRs que o monitor **de fato revisaria** (mesma regra de dono do estágio 3 — `_owns_pr_branch`: branch `auto/issue-*`, ou qualquer uma com `enable_review_human_prs`). Antes rotulava toda PR aberta, e a de branch alheio ficava presa em `~review:pendente` para sempre (o review nunca a reivindica). Além disso, o lock `~batch:<sha>` na **classificação** (issues e PRs) só é reivindicado/limpo quando há mais de um monitor (`shard_count > 1`); com monitor único — o caso do cluster — o claim apenas adicionava e removia o label na mesma passada (ruído de timeline), sem proteger contra nada. |
| Evidência | `deile/orchestration/pipeline/stages.py` (`classify_new_prs` com guarda `_owns_pr_branch` + `multi_monitor`; `classify_new_issues` com `multi_monitor`); `deile/tests/orchestration/pipeline/test_pr_triage.py`, `test_gap_regressions.py` (`TestGap6Stage0UsesClaim`) — PR #264 |
| Motivação | (1) Triagem e revisão precisam concordar sobre o que está na fila — senão PR alheia fica "pendente" eterna; (2) o lock só protege contra corrida entre monitores paralelos — com réplica única é puro churn cosmético na timeline da issue/PR. |

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
