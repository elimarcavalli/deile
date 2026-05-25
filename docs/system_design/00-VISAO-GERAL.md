# 00 — Visão Geral do System Design

> **Único índice e fonte de verdade para contagens.** Nenhum outro documento deste diretório armazena totais ou catalogações. Todos referenciam este arquivo.

## Identificação do projeto

| Campo | Valor |
|---|---|
| Nome | DEILE |
| Tipo | Agente autônomo de desenvolvimento, modo CLI |
| Linguagem principal | Python 3.9+ |
| Ponto de entrada | `python3 deile.py` (raiz) |
| Classe-bootstrap | `DeileAgentCLI` (em `deile.py`) |
| Configuração de testes | `pytest.ini` (raiz) |

## Pilares do System Design

| # | Pilar | Documento | Responsabilidade única |
|---|---|---|---|
| 1 | Capacidades operacionais | [`01-CAPACIDADES.md`](01-CAPACIDADES.md) | O que DEILE faz, em termos funcionais |
| 2 | Arquitetura de alto nível | [`02-ARQUITETURA.md`](02-ARQUITETURA.md) | Camadas, subpacotes, dependências |
| 3 | Princípios arquiteturais | [`03-PRINCIPIOS-ARQUITETURAIS.md`](03-PRINCIPIOS-ARQUITETURAIS.md) | Regras inegociáveis (hexagonal, registry, async, segurança) |
| 4 | Modelo de componentes | [`04-MODELO-COMPONENTES.md`](04-MODELO-COMPONENTES.md) | Tools, Commands, Parsers, Personas — interfaces e registries |
| 5 | Fluxo de execução | [`05-FLUXO-EXECUCAO.md`](05-FLUXO-EXECUCAO.md) | Loop do agente, intent analysis, orquestração, workflow |
| 6 | Memória | [`06-MEMORIA.md`](06-MEMORIA.md) | Quatro camadas (working/episodic/semantic/procedural) |
| 7 | Integrações LLM | [`07-INTEGRACOES-LLM.md`](07-INTEGRACOES-LLM.md) | Multi-provider, tier router, circuit breaker, budget |
| 8 | Segurança | [`08-SEGURANCA.md`](08-SEGURANCA.md) | Permissões, audit log, scanner de segredos, sistema de aprovação |
| 9 | Configuração | [`09-CONFIGURACAO.md`](09-CONFIGURACAO.md) | Settings singleton, YAML/JSON, env vars, hot-reload |
| 10 | Diagramas consolidados | [`10-DIAGRAMAS.md`](10-DIAGRAMAS.md) | Componentes, sequência, dependências em ASCII |
| 11 | Workflow de desenvolvimento | [`11-WORKFLOW-DESENVOLVIMENTO.md`](11-WORKFLOW-DESENVOLVIMENTO.md) | Tiers de escopo (Trivial/Small/Medium/Large) e fases |
| 12 | Padrões de código | [`12-PADROES-CODIGO.md`](12-PADROES-CODIGO.md) | Templates concretos para criar/editar artefatos |
| 13 | Padrão de documentação | [`13-PADRAO-DOCUMENTACAO.md`](13-PADRAO-DOCUMENTACAO.md) | Template das 14 seções para `docs/<data>_FEATURE.md` |
| 14 | Containerização (K8s) | [`14-CONTAINERIZACAO.md`](14-CONTAINERIZACAO.md) | Three init modes (Local / Job / deile-shell); isolation model |
| — | Registro de decisões | [`DECISOES.md`](DECISOES.md) | Decisões arquiteturais com histórico |

## Fonte única de verdade — onde cada fato vive

| Fato | Documento dono | Como outros docs devem referenciar |
|---|---|---|
| Decisões arquiteturais (resumo + tabela) | Este arquivo, seção "Decisões" | "ver `00-VISAO-GERAL.md`" |
| Decisões arquiteturais (detalhe + histórico) | [`DECISOES.md`](DECISOES.md) | "ver `DECISOES.md` #N" |
| Lista de tools, comandos, parsers, personas | Inventário do código (`ls`/`grep`) | Documentos descrevem responsabilidades, **não listam itens** |
| Modelos LLM disponíveis e tiers | [`deile/config/model_providers.yaml`](../../deile/config/model_providers.yaml) | "ver `model_providers.yaml`" |
| Padrões de intenção | [`deile/config/intent_patterns.yaml`](../../deile/config/intent_patterns.yaml) | "ver `intent_patterns.yaml`" |
| Configuração de personas | [`deile/config/persona_config.yaml`](../../deile/config/persona_config.yaml) e [`deile/personas/library/*.yaml`](../../deile/personas/library) | "ver `persona_config.yaml`" |
| Instruções de personas (prosa) | [`deile/personas/instructions/*.md`](../../deile/personas/instructions) | "ver `personas/instructions/`" |
| Versão do projeto | [`deile/__version__.py`](../../deile/__version__.py) | "ver `__version__.py`" |
| Datas de alteração | `git log` / `git blame` | **Nunca** manter manualmente |

## Inventário (referencia código, sem contagens hardcoded)

> Todos os números abaixo são determinados em runtime por `ls` ou pelo loader correspondente. Não copie valores aqui — abra a fonte.

| Categoria | Fonte autoritativa | Comando para inventariar |
|---|---|---|
| Subpacotes do `deile/` | filesystem | `ls deile/` |
| Tools | filesystem | `ls deile/tools/*.py` (excluindo `base.py`, `registry.py`, `__init__.py`) |
| Comandos slash | filesystem | `ls deile/commands/builtin/*.py` (excluindo `__init__.py`) |
| Parsers | filesystem | `ls deile/parsers/*.py` (excluindo `base.py`, `registry.py`, `__init__.py`) |
| Camadas de memória | filesystem | `ls deile/memory/*.py` (excluindo `memory_manager.py`, `memory_consolidation.py`, `__init__.py`) |
| Provedores de LLM | YAML | seção `providers:` em `deile/config/model_providers.yaml` |
| Modelos | YAML | seção `models:` em `deile/config/model_providers.yaml` |
| Personas (instruções) | filesystem | `ls deile/personas/instructions/*.md` |
| Personas (configurações) | filesystem | `ls deile/personas/library/*.yaml` |
| Skills bundled | filesystem | `find deile/skills/library -name '*.md'` |
| Skills do usuário / projeto | filesystem | `find ~/.deile/skills <cwd>/.deile/skills -name '*.md' 2>/dev/null` (mais paths em `SettingsManager.get_all_skills_paths()`) |
| Profiles de configuração | filesystem | `ls deile/config/profiles/*.yaml` |

## Decisões — tabela-resumo

> Detalhe completo de cada decisão (motivação, evidência, histórico) vive em [`DECISOES.md`](DECISOES.md). A tabela abaixo é apenas índice.

| # | Decisão (resumo) | Versão | Pilar dono |
|---|---|---|---|
| 1 | CLI single-binary com bootstrap condicional de providers | V1 | Arquitetura (02) |
| 2 | Pelo menos uma chave de API de LLM é requerida no startup | V1 | Configuração (09) |
| 3 | Registry Pattern para tools, comandos, parsers, personas | V1 | Componentes (04) |
| 4 | Async/await obrigatório em toda I/O | V1 | Princípios (03) |
| 5 | Arquitetura hexagonal (core ↔ adapters em `infrastructure/`) | V1 | Princípios (03) |
| 6 | Memória em quatro camadas (working/episodic/semantic/procedural) | V1 | Memória (06) |
| 7 | Multi-provider com `ModelRouter` legado e `TierRouter` por tiers | V1 | Integrações LLM (07) |
| 8 | Circuit breaker por provider e budget por sessão/diário/mensal | V1 | Integrações LLM (07) |
| 9 | Sistema de permissões baseado em regras + audit logging tipado | V1 | Segurança (08) |
| 10 | Sistema de aprovação por nível de risco em planos | V1 | Segurança (08) |
| 11 | `Settings` como singleton via `get_settings()` | V1 | Configuração (09) |
| 12 | Personas instanciadas por instruções em Markdown + YAML de capacidades | V1 | Componentes (04) |
| 13 | Hot-reload de configuração e plugins via `watchdog` | V1 | Configuração (09) |
| 14 | Persistência (memória episódica/semântica/uso) em SQLite | V1 | Memória (06), Integrações (07) |
| 15 | Streaming-first: `process_input_stream` é o caminho default da CLI | V1 | Fluxo (05) |
| 16 | Two-flag flag de fallback `use_legacy_gemini_only` em `model_providers.yaml` | V1 | Integrações LLM (07) |
| 17 | Separação `deile`/`deilebot` + protocolo HTTP local (Bearer, 127.0.0.1) para a flecha reversa `agente → bot` | V1 | Arquitetura (02), Componentes (04), Segurança (08) |
| 18 | Hash sharding para execução paralela de monitores (`MonitorIdentity` + `shard_index/shard_count`) | V1 | Princípios (03), Arquitetura (02) |
| 19 | Cron genérico separado do scheduler do pipeline (`CronStore` SQLite + `CronRunner` vs `ScheduleStore` YAML) | V1 | Componentes (04) |
| 20 | Strip de `ANTHROPIC_API_KEY` no subprocess do Claude Code (`ClaudeDispatcher.prefer_subscription_auth`) | V1 | Segurança (08) |
| 21 | Schedule padrão completo + fallback legacy para stages ausentes (`tick()` gap #1 — issue #129) | V1 patch | Arquitetura (02), Configuração (09) |
| 22 | Stage 1 atômico com rollback `em_revisao → nova` em caso de falha (gap #13 — issue #129) | V1 patch | Princípios (03) |
| 23 | Batch ID derivado do número (não título) via `compute_batch_id_for_number` (gap #10 — issue #129) | V1 patch | Arquitetura (02) |
| 24 | TOCTOU mitigation em `claim_with_batch`: re-fetch após `add_labels` para detectar race condition (gap #11 — issue #132) | V1 patch | Princípios (03), Arquitetura (02) |
| 25 | Comandos slash declaram CLI flags via metadata (`cli_flag`/`cli_extra_flags`); argparse é gerado pelo registry — issue #126 | V1 | Componentes (04), Arquitetura (02) |
| 26 | Project layer de `.deile/settings.json` exige opt-in via `trust.project_layer_dirs` + permission/audit em `set_setting` (issue #125) | V1 patch | Segurança (08), Configuração (09) |
| 27 | Stack de containerização em K8s (Rancher Desktop / k3s) para isolar deile-Job/bot/deile-shell do host — secrets como files (não env), pop após bootstrap, NetworkPolicy default-deny, PSS restricted, drop ALL caps | V1 | Containerização (14), Segurança (08) |
| 28 | Tool whitelist no agente embutido do bot e default-`messaging` no `deile-oneshot` Job — Discord input é untrusted, prompt do Job é fixo; toolset cheio só no `deile-shell` interativo (prompt vem do operador via kubectl exec) | V1 | Containerização (14), Componentes (04) |
| 29 | Permission gate + audit logging do `dispatch_deile_task` adiados para feature dedicada — refator hexagonal isolado; compensado por tool whitelist (#28), NetworkPolicy (#27) e cooldown de 30s | V1 | Segurança (08) |
| 30 | Resume de trabalho parcial no pipeline (in-place no PVC, sem `reset --hard`); detecção de fim ground-truth-first; guarda de progresso por fingerprint substantivo; teto de tentativas/orçamento; `~workflow:bloqueada` exclui do auto-resume — issue #254 | V1 | Arquitetura (02), Fluxo (05), Segurança (08) |
| 31 | `PipelineImplementer` como estratégia plugável (`ClaudeImplementer` via `claude -p` **vs** `WorkerImplementer` que despacha ao `deile-worker` por HTTP), selecionada por `dispatch_mode` — torna o Claude opcional no loop autônomo DEILE-a-DEILE — issue #255 | V1 | Arquitetura (02), Componentes (04) |
| 32 | Roteamento de menção/atribuição por papel (`process_mentions` é roteador): issue+assignee/body → injeta `~workflow:nova`; PR+assignee → review+merge; PR+reviewer-só → revisa e devolve ao autor sem mergear; comment → atende ao pedido. Idempotência cross-tick via `~mention:processado`; review de PR sob a persona `reviewer` (quality-gate SOLID/SRP/segurança, não só testes verdes) — issues #253/#261 | V1 | Fluxo (05), Componentes (04), Segurança (08) |
| 33 | Triagem de PR só rotula `~review:pendente` em branch que o monitor revisaria (`auto/issue-*`, ou qualquer com `enable_review_human_prs`); lock `~batch:` na classificação só é reivindicado quando `shard_count>1` (monitor único não gera churn) — PR #264 | V1 patch | Arquitetura (02), Princípios (03) |
| 34 | Sub-DEILEs paralelos em sessão CLI (decomposição autônoma): tool `dispatch_parallel_subagents` → `SubAgentOrchestrator` (asyncio.gather/return_exceptions) com runner pluggable (Local in-process default; Worker via HTTP `wait=False`+polling) + painel Rich Live multipanel ~5 linhas/frente com foco básico por tecla numérica; novo endpoint `GET /v1/progress/{task_id}` no `deile-worker` para snapshot mid-flight — issue #257 | V1 | Arquitetura (02), Componentes (04), Fluxo (05) |
| 35 | Sistema unificado de **Skills** como quinto componente plugável (MD com frontmatter YAML, sem código Python): scan de 5 diretórios (bundled + user + claude/commands + project + extras), três caminhos de ativação (auto-injeção no system prompt via `triggers`, function-call `invoke_skill`/`list_skills`, slash `/<name>`), hot-reload por `watchdog` com swap atômico via `SkillRegistry.replace_all`, path-traversal containment em `file_content_patterns`, registry singleton thread-safe (`RLock` + double-checked locking) — PR #296 | V1 | Componentes (04), Fluxo (05), Padrões de código (12) |
| 36 | Helpers `aio_fileio` (`read_json` / `write_json` / `write_text`) em `deile/storage/` para isolar I/O bloqueante de paths `async`. Formatos domain-specific (JSONL, YAML estruturado) ficam locais ao subpacote dono — PR #298 | V1 patch | Princípios (03), Arquitetura (02) |

## Estado dos pilares

| Pilar | Status |
|---|---|
| 01 Capacidades | concluido |
| 02 Arquitetura | concluido |
| 03 Princípios | concluido |
| 04 Componentes | concluido |
| 05 Fluxo | concluido |
| 06 Memória | concluido |
| 07 Integrações LLM | concluido |
| 08 Segurança | concluido |
| 09 Configuração | concluido |
| 10 Diagramas | concluido |
| 11 Workflow | concluido |
| 12 Padrões de código | concluido |
| 13 Padrão de documentação | concluido |
| 14 Containerização | concluido |
