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
