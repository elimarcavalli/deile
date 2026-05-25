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

## Decisão #34 — Sub-DEILEs paralelos em sessão CLI (decomposição autônoma)

| Campo | Valor |
|---|---|
| Versão | V1 (refatorada em iterações sucessivas — ver Histórico) |
| Pilar dono | 02-Arquitetura, 04-Componentes, 05-Fluxo |
| Decisão | Durante uma sessão interativa CLI, o DEILE decompõe autonomamente uma solicitação em sub-tarefas independentes e substanciais e dispara **N sub-DEILEs em paralelo** (cada um com sessão limpa). O LLM principal chama a tool **`dispatch_parallel_subagents`** com lista de 2-5 `{description, prompt, persona?, model?}`. A tool delega ao **`SubAgentOrchestrator`** (asyncio.create_task + wait FIRST_COMPLETED + drain, com semaphore de `max_parallel` e budget global via `wait_for`), que escolhe o runner por config (`subagent_runner`): **`LocalSubAgentRunner`** (default — in-process via `DeileAgent.process_input_stream(_skip_autonomous=True)` com `session_id` próprio) ou **`WorkerSubAgentRunner`** (delega ao `deile-worker` via `DeileWorkerClient.dispatch(wait=False)` + polling de `GET /v1/progress/{task_id}`). Ambos herdam de **`_BaseRunner`** (template-method: cada subclasse implementa só `_do_work` + opcional `_finalize`; o ciclo `running → started_at → STARTED event → cancel/exception handling → mark_*` é compartilhado). A UX é um painel Rich Live multipanel (~5 linhas/frente, refresh 6Hz) com **foco básico** (tecla numérica abre ficha com `description`/`prompt`/`persona`/`model`/`task_id` + tail do stream; ESC volta). Falha de uma frente não cancela siblings. Stdout/stderr redirecionados para `_CappedBuffer` (cap 256KiB/stream) durante a execução — `print()` de sub-DEILEs não polui o terminal; painel mantém ref ao stdout REAL. Histórico marca entradas display-only com `subagent_panel_summary=True` em metadata; `ContextManager.build_context` filtra (evita Anthropic 400 sob duas assistants seguidas); `replay_history` renderiza no `/resume`. Consolidação final é responsabilidade do LLM principal (recebe resumo agregado pelo tool). |
| Evidência | `deile/orchestration/subagents/{__init__,orchestrator,runner,events,constants,_capture,_loop_lock,_summary}.py`; `deile/tools/dispatch_parallel_subagents.py`; `deile/ui/{subagent_panel.py,_stdin_owner.py}`; `deile/core/{agent.py, agent_streaming.py (_skip_autonomous kwarg), context_manager.py (filtro display-only)}`; `deile/cli_session_helpers.py` (replay do painel); `deile/infrastructure/deile_worker_client.py` (`get_progress`/`get_result`); `infra/k8s/worker_server.py` (endpoint `GET /v1/progress/{task_id}` + `_evict_old_tasks_if_needed` + progresso mid-flight no `_TASKS[id]`); `deile/personas/instructions/developer.md`. Testes: `deile/tests/orchestration/test_subagent_*`, `deile/tests/tools/test_dispatch_parallel_subagents.py`, `deile/tests/ui/{test_subagent_panel.py,test_stdin_owner.py}`, `deile/tests/infra/test_worker_progress_endpoint.py`, `deile/tests/core/test_context_manager_subagent_filter.py`. Issue #257; demo end-to-end em `test-your-might/issue-257-demo/`. |
| Motivação | (1) Tarefas decomponíveis (refator multi-módulo, geração de testes multi-arquivo, doc + impl separáveis) caem do tempo sequencial ao tempo da frente mais lenta; (2) infra de workers (`dispatch_deile_task` + `deile-worker`) subutilizada por depender de Discord — agora também serve a sessão CLI; (3) experiência: o usuário **vê** o DEILE em múltiplas frentes (bash/tool atual por painel, contador, foco), o que dá percepção de potência e confiança; (4) runner pluggable atende dois ambientes (laptop local — runner local sem infra; pod no cluster — runner worker reusando o load-balancer multi-réplica). |
| Fora do escopo | Decomposição recursiva (sub-DEILE que dispara outros — bloqueado pelo `_NESTING_DEPTH` ContextVar; 1 nível por enquanto); garantias transacionais entre sub-tarefas (workspace compartilhado no runner local — conflito de escrita é risco a tratar, não resolvido aqui); SSE real-time puro (polling de snapshot é aceitável conforme proposta de viabilidade da issue); paralelismo entre requisições de usuários distintos. |

### Histórico
- **V1.0** (PR #295): release inicial. Tool, orchestrator, runners local+worker, painel Rich, endpoint `/v1/progress/{task_id}` no worker, persona doc, 50+ testes unitários.
- **V1.1** (review post-PR): blockers — race no redirect de `sys.stdout` (lock por-event-loop), GC do `_TASKS` no worker, leak no event bus; majors — settings wiring (`subagent.*` em `_OVERRIDE_HANDLERS`), audit log no dispatch, persona warning visível, path-traversal nos handlers, `_get_json` ordering, atexit termios safety-net, lazy `_lock` no `_lazy_lock_for_loop`.
- **V1.2** (teste end-to-end com `deepseek-v4-flash`): bug fundamental encontrado — sub-DEILEs entravam no `autonomous`/`workflow` path do `process_input_stream`, yieldando só TEXT_DELTA/USAGE_FINAL (sem TOOL_USE_END) → painel mostrava `(sem atividade ainda)` o turno todo apesar dos sub-DEILEs criarem arquivos. Fix: kwarg `_skip_autonomous=True` no `process_input_stream` força a ida direto ao chat-with-tools loop.
- **V1.3** (simplify pass): ~21% de redução (-586 linhas), aplicando SRP/KISS — `_BaseRunner` template-method elimina duplicação entre runners; `_lazy_lock_for_loop` reusada entre orchestrator e tool; `_make_renderer`/`_await_or_orphan` extraídos do `_run_locked`; parser de teclado quebrado em `_apply_key`/`_read_byte`/`_drain_escape_sequence`; cancel labels consolidados em ClassVars; anotações verbose de iterações de review removidas (informação WHY preservada terse).
- **V1.4** (review pós-V1.3, PR #300): extração de helpers `_capture.py` (capture stdout/stderr isolado), `_loop_lock.py` (LoopBoundLock + threading.Lock guard), `_summary.py` (formatadores) — refactor adicional aplicando DRY + dead-code removal; settings wire `subagent.capture_buffer_max_bytes`.
- **V1.5** (round 5, este PR #306): Rich Live `FileProxy` sobrescrevia `sys.stdout` redirect → fix com `Live(redirect_stdout=False)`; `text_segments` com join `\n\n` para preservar parágrafos no `/resume`; persona warning explícita que tool funciona sempre local (LLM confundia com `dispatch_deile_task`).

---

## Decisão #35 — Sistema unificado de Skills como quinto componente plugável

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 04-Componentes, 05-Fluxo, 12-Padrões de código |
| Decisão | **Skill = arquivo Markdown com frontmatter YAML**, sem código Python. Vive em um de 5 diretórios escaneados em ordem de prioridade crescente (bundled em `deile/skills/library/` → `~/.deile/skills/` → `~/.claude/commands/` UPPERCASE → `<cwd>/.deile/skills/` → `<cwd>/.claude/commands/` UPPERCASE + extras de `SettingsManager`). Três caminhos de ativação simultâneos: (a) **auto-injeção** no system prompt do turno quando uma `trigger` casa (`file_globs`, `code_block_langs`, `keywords`, `file_content_patterns`), via `SkillRouter.select_skills` chamado por `ContextManager._build_skills_block`; (b) **function-call tools** `invoke_skill(name)` e `list_skills` em `deile/tools/skill_tools.py` (auto-descobertos via `DEFAULT_TOOL_PACKAGES`) que o LLM pode chamar quando vê no catálogo (`SkillRouter.render_catalog`) uma skill aplicável ao tópico; (c) **slash command** `/<name>` para invocação explícita do usuário, via shim de backward-compat `deile/commands/skill_loader.py`. `SkillRegistry` é singleton thread-safe (`RLock` + double-checked locking; `replace_all` para swap atômico durante hot-reload). `SkillsWatcher` (`watchdog.Observer`) refaz o registry em 0,5 s a cada `.md` event, serializado por `_RELOAD_LOCK`. Path-traversal containment em `file_content_patterns` via `router._resolve_within` impede que skill maliciosa probe `/etc/passwd` por crafted `file_references`. |
| Evidência | `deile/skills/` (10 módulos: base, loader, discovery, registry, router, watcher, bootstrap, slash_command_bridge, config, language_detector); `deile/skills/library/{languages/python,languages/typescript,practices/tdd}.md` (skills bundled); `deile/tools/skill_tools.py` (InvokeSkillTool + ListSkillsTool); `deile/commands/skill_loader.py` (shim legacy); `deile/core/agent.py` (boot do `SkillsWatcher` em `_auto_discover_components` + `stop()` em `shutdown`); `deile/core/context_manager.py:_build_skills_block` (injeção no system prompt). Testes: `deile/tests/skills/` (209 testes) + `deile/tests/test_skill_loader.py` + `deile/tests/commands/test_skills_command.py`. PR #296. |
| Motivação | (1) Permitir que o usuário/projeto adicione expertise específica do projeto **sem mudar código Python** (paridade com Claude Code skills/`.claude/commands/`); (2) unificar o que antes eram dois sistemas distintos (slash commands legados do PR #41 e skills "especialistas" novas) num único registry — evita duplicação de loader, override e parsing; (3) três caminhos de ativação cobrem três casos de uso reais: auto-injeção para skills que cobrem padrões frequentes do projeto, `invoke_skill` para o LLM puxar sob demanda quando vê o catálogo, slash command para invocação explícita pelo usuário; (4) o catálogo no system prompt (com diretiva imperativa + exemplo concreto) é o que dispara o `invoke_skill` espontâneo do LLM — empiricamente validado em 4/5 probes contra `deepseek:deepseek-v4-flash`. |
| Fora do escopo | (1) Extração automática de `file_references` de texto livre (limitação pré-existente do `FileParser` — não bloqueia auto-trigger por `file_globs` quando o parser sí extrai); (2) sandbox de execução para skills (skills só injetam texto; não rodam código); (3) versionamento/depend tree entre skills. |

### Histórico

| Data | Mudança |
|---|---|
| Inicial (PR #296) | Sistema unificado entregue, com diretrizes de hardening (thread-safety, path traversal, CRLF, bool priority, missing-config-defaults-enabled) e simplificação (cut de ~660 linhas vs primeira iteração; `bootstrap_skills_with_handle` como canonical entry point, `bootstrap_skills` como wrapper legacy) |

---

## Decisão #36 — Helpers `aio_fileio` em `deile/storage/` para isolar I/O bloqueante de paths `async`

| Campo | Valor |
|---|---|
| Versão | V1 patch |
| Pilar dono | 03-Princípios, 02-Arquitetura |
| Decisão | I/O bloqueante (`open()`, `json.dump`, `json.load`, `f.write`) chamado de dentro de `async def` viola o princípio 03 §1 ("I/O bloqueante proibido em contexto async"). Onde múltiplos subpacotes precisam do mesmo round-trip (JSON dict, texto), centralizar em `deile/storage/aio_fileio.py` (`read_json` / `write_json` / `write_text`) — cada helper é um one-liner `await asyncio.to_thread(<sync_fn>, ...)`. Subpacotes consomem via `from deile.storage.aio_fileio import ...`, não redefinem helpers locais. Formatos domain-specific (JSONL append em `semantic_memory`, mutação de estrutura YAML em `config/manager`) ficam **locais** — pertencem ao domínio que conhece o esquema, não ao módulo genérico. |
| Evidência | `deile/storage/aio_fileio.py` (3 funções públicas); call sites em `deile/orchestration/approval_system.py` (`_save_request`, `_load_request`, `list_requests`) e `deile/orchestration/plan_manager.py` (`load_plan`, `list_plans`, `_save_plan`, `_save_plan_markdown`); auditoria que motivou a fix em `docs/system_design/03-PRINCIPIOS-ARQUITETURAIS.md` §1. |
| Motivação | (1) Cumprir o princípio inegociável de async-first sem proliferação de helpers `_read_json` privados em cada arquivo (5 cópias near-idênticas antes da consolidação); (2) Reuso máximo + SRP: o módulo expõe apenas os primitivos genéricos, formatos especializados permanecem com seu dono lógico. |
| Histórico | Introduzido durante o bug-audit PR #298 (sweep com 4 auditores sonnet) — eliminou 5 helpers duplicados em `approval_system.py` e `plan_manager.py`. |

---

## Decisão #37 — Runtime state por-processo via state file + heartbeat (substitui inferência por log no painel)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 08-Segurança |
| Decisão | Cada processo DEILE (CLI interativo, deile-pipeline, deile-worker, deilebot, deile-shell) publica seu **estado vivo autoritativo** em `~/.deile/run/<instance_id>.json` — `instance_id = <role>-<uuid4[:8]>`. O painel TUI universal (`infra/k8s/_panel*`, PR #294) deixa de **inferir** "doing now" por log-tailing global (que não atribui linha→PID e quebra em DEBUG suprimido / rotação / múltiplos writers) e passa a **ler** essa fonte de verdade por processo. Escrita atômica: `<id>.json.tmp` + `os.replace` (atômico em POSIX; "atômico o suficiente" em Windows). Schema versionado (`schema_version=1`) com `pid`, `role`, `started_at`, `last_heartbeat_at`, `current_action ∈ {idle, starting, tool_execution, llm_call, shutting_down}` (com `detail` truncado em 80 chars, `session_id`/`model` opcionais e opacos), e `stats` acumuladores (`tokens_in`, `tokens_out`, `cost_usd`, `turns`, `tool_calls`, `errors`). Subpacote novo `deile/runtime/` **separado** de `deile/memory/` por contrato: memória é persistente, por-sessão, com camadas de propósito; runtime state é volátil, por-processo, e expõe metadados de execução. Singleton `get_instance_state(role)` + injeção opcional via construtor; heartbeat é uma task asyncio que atualiza `last_heartbeat_at` a cada `interval_s` (default 2.0s) e re-raise em `asyncio.CancelledError` (princípio 6). `atexit` registra `close()` (idempotente, remove o arquivo). **Sem segredos** em nenhum campo: nada de `tool_args`, prompts, respostas de LLM ou paths absolutos do `$HOME` — regra do pilar 08. Integração: `_DeileCLI.initialize()` cria o singleton com `role="cli"`, marca `starting`, agenda heartbeat e cancela limpa no `run_interactive` finally; `DeileAgent._execute_tools` e `ToolLoopExecutor` (streaming) atualizam `current_action`/`stats` em volta de cada execução de tool; `_stream_chat_with_tools` marca `llm_call` durante o stream; `process_input`/`process_input_stream` incrementam `turns`. Toda chamada ao runtime state é best-effort (`try/except` mudo) — observability nunca quebra o turn. Fases 2-4 (Unix socket `/status`, registry compartilhado, OTLP exporter) ficam como **roadmap** explícito na issue, não implementadas neste PR. |
| Evidência | `deile/runtime/__init__.py`, `deile/runtime/instance_state.py` (`InstanceState`, `get_instance_state`, `reset_instance_state`, `pid_alive`, `VALID_ROLES`, `VALID_ACTION_KINDS`, `DETAIL_MAX_LEN`, `SCHEMA_VERSION`); `deile/cli.py` (`_DeileCLI.__init__`/`initialize`/`_shutdown_instance_state`); `deile/core/agent.py` (`_execute_tools`, `_stream_chat_with_tools`, `process_input`, `process_input_stream`); `deile/core/tool_loop_executor.py` (wrap em volta do `tool_registry.execute_tool`); `deile/tests/runtime/test_instance_state.py` (39 testes — schema/atomic/heartbeat/CancelledError/idempotência/atexit/pid_alive/singleton); `deile/tests/runtime/conftest.py` (isolamento via `DEILE_RUNTIME_DIR` + reset do singleton) — Fase 1 da issue #303 |
| Motivação | (1) Atribuição linha→PID não é resolvível por log global sem mudar o formato do logger inteiro — e ainda assim sofre de race, lossy DEBUG, rotação e ambiguidade quando o processo morre; (2) state file + heartbeat são **publicação ativa**: cada processo é a fonte de verdade do seu próprio estado, sem regex frágil no painel; (3) o painel ganha "doing now" correto por processo sem que o DEILE precise expor RPC (Fase 2); (4) o roadmap (Unix socket → registry → OTLP) cresce sobre o mesmo schema sem rework. |
| Fora do escopo | Unix socket `/status` (Fase 2), registry/service discovery (Fase 3), OTLP exporter (Fase 4) — todos documentados no corpo da issue #303 e adiados até haver fleet/observabilidade enterprise demandando-os. Reentrância de restart (mesmo PID adotar state file existente) também adiada — `atexit` cobre o caminho limpo; órfãos serão GC pelo painel via `pid_alive`. |

---

## Decisão #38 — Status server (Unix socket) + Registry compartilhado para o runtime state

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 02-Arquitetura, 08-Segurança |
| Decisão | Evolui a #37 com **Fase 2 (Unix socket `/status`)** e **Fase 3 (registry)** da issue #303. **(Fase 2 — Status server)** cada processo DEILE expõe um endpoint local em `<runtime_dir>/<instance_id>.sock` com protocolo line-based (`STATUS\n` → JSON snapshot; `METRICS\n` → exposição Prometheus textual; `FLUSH\n` → `OK` (debug); qualquer outra coisa → `ERR …` + close). Servidor asyncio-native (`asyncio.start_unix_server`), tasks iniciadas via `InstanceState.start_async_tasks()` junto com o heartbeat. Permissão restritiva (`chmod 0o600`) aplicada após bind para isolar do tráfego cross-user. Limites defensivos: linha máx. 1KB, NUL byte rejeitado, 1 request por conexão (sem keep-alive — simplifica vida e mantém latência baixa). Cliente síncrono (`StatusClient`) por desenho — o painel TUI consome em threads do `BackgroundRefresher`; tornar async exigiria embed de event loop por thread. Em Windows o servidor vira no-op silencioso (Unix socket é POSIX-only); o painel cai no caminho legado (state file). **(Fase 3 — Registry)** `<runtime_dir>/registry.json` lista todos os processos ativos (`instance_id`, `pid`, `role`, `started_at`, `endpoint`, `state_file`). Lock atômico via `fcntl.flock(LOCK_EX)` em POSIX (no-op em Windows — single-host, baixa contenção). GC inline em `Registry.list()`: entry é removida quando `pid_alive(entry.pid)=False` OU `state_file` ausente — cobre `kill -9`/crash. Atomicidade write-tmp + `os.replace`. Schema versionado (`schema_version=1`), forward-compat. **Integração no painel:** `LocalInstancesProvider` ganha caminho preferencial via socket (`_try_fetch_via_socket` antes de cair no file); novo `LocalRegistryProvider` opcional para mostrar "fleet view" no header. Imports tolerantes ao pacote `deile` ausente — o painel continua standalone. **Integração no agente:** `InstanceState.__init__` aceita `enable_status_server=True` e `enable_registry=True` (defaults True); instancia ambos; `start_async_tasks()` orquestra heartbeat + serve_forever em uma lista de tasks; `close()` deregista (síncrono) + para o status server (best-effort: agenda `loop.create_task(server.stop())` se há loop ativo, senão só apaga o socket file — o OS limpa o resto). `_DeileCLI.initialize` migra de criar a task de heartbeat manualmente para chamar `start_async_tasks()`; `_shutdown_instance_state` para o servidor explicitamente antes de cancelar a lista de tasks (evita `serve_forever` cancelar no meio de uma resposta). Fase 4 (OTLP exporter) continua no roadmap futuro. |
| Evidência | `deile/runtime/status_server.py` (`StatusServer`, `StatusClient`, `format_metrics`, `MAX_LINE_BYTES`); `deile/runtime/registry.py` (`Registry`, `RegistryEntry`, `REGISTRY_SCHEMA_VERSION`); `deile/runtime/instance_state.py` (`start_async_tasks`, `_build_registry_entry`, `_shutdown_status_server_best_effort`, novos kwargs `enable_status_server`/`enable_registry`); `deile/runtime/__init__.py` (reexports); `deile/cli.py` (`_DeileCLI.__init__`/`initialize`/`_shutdown_instance_state` migrados para lista de tasks); `infra/k8s/_panel_data.py` (`LocalInstancesProvider._try_fetch_via_socket`, `LocalRegistryProvider`, `RegistrySnapshot`, hookups em `PanelData.from_context`/`_all_providers`/`errors`); `deile/tests/runtime/test_status_server.py` (23 testes — lifecycle, perm 0600, STATUS/METRICS/FLUSH, error handling, client síncrono); `deile/tests/runtime/test_registry.py` (19 testes — register/deregister/list, GC dead PID + missing state_file, JSON corrompido, schema_version desconhecido, file-lock cross-thread, atomic write); `deile/tests/runtime/conftest.py` (`short_runtime_dir` fixture — `/tmp/dx-<hex>` para evitar `AF_UNIX path too long` em macOS). Smoke manual: `echo STATUS \| nc -U ~/.deile/run/<id>.sock` devolve JSON; `METRICS` devolve texto Prometheus parseável; permissão do socket é `0o600` confirmada por `stat`. |
| Motivação | (1) Painel passa a mostrar estado **mais novo** que o último flush do state file — o socket entrega o `current_action` instantâneo, eliminando a janela de 2s do heartbeat; (2) `METRICS` em formato Prometheus prepara o terreno pra Fase 4 (OTLP) sem dependência nova — scrapers genéricos já funcionam; (3) registry permite "fleet view" sem varrer o filesystem ou inferir contagem; (4) GC do registry e GC dos state files cobrem cenários distintos (registry sem heartbeat / state file órfão) — defesa em profundidade contra `kill -9`; (5) tudo opt-in por kwargs — testes existentes podem desabilitar (`enable_status_server=False`) para não levantar sockets desnecessários, e em Windows o agente continua rodando com Fase 1 sozinha sem warning ruidoso. |
| Fora do escopo | Auth/encryption no socket — `0o600` é o controle suficiente para single-user (não há multi-tenant no painel local). Watchdog/inotify sobre o registry para mudanças cross-process — o cliente do registry tem cache TTL no painel (3s) e o caso de uso não exige push. Reentrância de restart no mesmo PID (state file pré-existente adotado pelo novo processo) também adiada — `atexit` cobre o caminho limpo, `~workflow:bloqueada` análogo não se aplica aqui. Painel ainda não invoca `StatusClient` diretamente para `FLUSH` (apenas integra `STATUS`); `FLUSH` fica como debug hook para operadores via `nc`. |

---

## Decisão #39 — Observabilidade enterprise via OpenTelemetry (Fase 4 da issue #303)

| Campo | Valor |
|---|---|
| Versão | V1 |
| Pilar dono | 11-Observabilidade, 02-Arquitetura, 08-Segurança |
| Decisão | Subpacote novo `deile/observability/` expõe `get_tracer()` / `get_metrics()` — um wrapper sobre OpenTelemetry (traces + metrics) com **fallback no-op** quando o SDK não está instalado OU `DEILE_OTLP_ENDPOINT` não está set OU `DEILE_OBSERVABILITY_DISABLED=true`. Schema CNCF padrão: spans `deile.turn` (1 por interação usuário→agente), `deile.tool.<name>` (1 por execução de tool, filho do turn) e `deile.llm.call` (1 por chamada a provider LLM). Métricas: `deile.tokens.total` (counter por provider/model/direction), `deile.cost.usd.total` (counter), `deile.tool.duration_ms` (histogram), `deile.turn.duration_ms` (histogram), `deile.errors.total` (counter). **Cardinality controlada**: `session_id` aparece como span attribute mas NUNCA como label de métrica; `deile.instance.id` / `deile.instance.role` (vindos do `InstanceState` da Fase 1) entram como resource attributes do tracer pra correlação cross-pod sem inflar labels por métrica. **Sem segredos** (pilar 08): atributos transportam apenas tamanhos (int), tokens (int), custo (float), latência (ms) e identificadores opacos — nenhum prompt, args, response, messages ou conteúdo livre. Integração: `DeileAgent.process_input`/`process_input_stream` abrem o span `deile.turn` no início e fecham no `finally` (helpers `_record_turn_error` / `_finalize_turn_span`); `ToolLoopExecutor` wrappa cada `execute_tool` em `deile.tool.<name>` e emite `deile.tool.duration_ms`; `ModelProvider.base._record_usage` (ponto único do flush de uso em todos os providers) emite `deile.tokens.total` + `deile.cost.usd.total`; providers (`anthropic`, `openai`, `gemini`) wrappam `generate()` / iterações do `chat_with_tools` em `with self._llm_span()` e populam `llm.tokens.in/out/cached`, `llm.cost_usd`, `llm.latency_ms` via `_set_llm_span_usage`. **Toda chamada de observability** é best-effort (`try/except Exception: pass` com `noqa BLE001`) — observability nunca quebra o turn. Dependências em extra opcional `[otel]` (não em `dependencies`) — `pip install -e ".[otel]"` é o único caminho de instalação. Singleton thread-safe (`_tracer_instance + threading.Lock`, mesmo padrão da Fase 1). Setup OTLP é **lazy**: `MeterProvider`/`TracerProvider` só são criados na primeira chamada que realmente emite — testes que não tocam observability não pagam custo. Config dedicada (`ObservabilityConfig` dataclass) lê env vars centralizadamente — núcleo não toca `os.environ` (princípio 7). |
| Evidência | `deile/observability/__init__.py`, `deile/observability/config.py` (`ObservabilityConfig.from_env()`), `deile/observability/no_op.py` (`NoOpTracer`/`NoOpMetrics`/`NoOpSpan`), `deile/observability/tracer.py` (`OtlpTracer`, `get_tracer`, `otel_available`), `deile/observability/metrics.py` (`OtlpMetrics`, `get_metrics`); `deile/core/models/base.py` (`_record_usage` emite tokens+cost; `_llm_span`/`_set_llm_span_usage`/`_set_llm_span_error` helpers); `deile/core/agent.py` (`_record_turn_error`/`_finalize_turn_span` + spans em `process_input`/`process_input_stream`); `deile/core/tool_loop_executor.py` (`_set_tool_span_status`/`_set_tool_span_error`/`_record_tool_metrics` + span wrap por tool); `deile/core/models/anthropic_provider.py` / `openai_provider.py` / `gemini_provider.py` (wrap `generate()` + `chat_with_tools` no `_llm_span()`); `pyproject.toml` `[project.optional-dependencies]` extra `otel`; `deile/tests/observability/` (37 testes — config/no-op/OtlpTracer com InMemorySpanExporter/OtlpMetrics com InMemoryMetricReader/integration com base+executor+agent helpers) — Fase 4 da issue #303 |
| Motivação | (1) `UsageRepository` (SQLite local) já cobre cost-tracking interno mas não correlaciona turn↔tool↔LLM-call num timeline visual quando algo dá errado em produção fleet; (2) padrão CNCF (OTLP→Tempo/Loki/Prometheus/Grafana) é a way-to-go para fleet >1 instance, e ter spans/métricas estruturadas é a única forma escalável de SRE responder "qual tool quebrou no turno X do usuário Y" sem ler logs; (3) extra opcional respeita usuários single-process que não querem o overhead — no-op é literal zero custo; (4) hookar em `_record_usage` (não em cada provider) é a dedup mais barata possível — uma única edição cobre os 4 providers atuais e qualquer novo. |
| Fora do escopo | Trace propagation (W3C traceparent) cross-process entre `deile-pipeline` → `deile-worker` — quando Fase 4 + Fase 2 do issue #303 (Unix socket) convergirem, o header naturalmente entra; logs estruturados com `trace_id`/`span_id` correlacionados (Loki) — depende de adapter no `deile.storage.logs.get_logger`, fora do escopo desta fase; instrumentação automática (`opentelemetry-instrumentation-httpx` etc.) — opcional, pode ser adicionada por extra adicional sem mexer no código; coletor Jaeger/Tempo de exemplo via docker-compose — documentado no roadmap mas não fornecido neste PR. |

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
