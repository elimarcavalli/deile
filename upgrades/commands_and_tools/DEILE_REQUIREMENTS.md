# D.E.I.L.E. — Requisitos Completos (MD)
> Versão: 1.0  
> Autor: Elimar (requisitos revisados e incrementados por D.E.I.L.E. assistant)  
> Objetivo: documento único com requisitos, tools, comandos, fluxos autônomos e plano de execução em etapas.  

---

## Sumário
1. Princípios e objetivos  
2. Visão geral da integração com Gemini (LLM)  
3. Comandos essenciais (CLI) — comportamento e UX  
4. Tools essenciais — contratos/schemas e comportamento de exibição (system-driven)  
5. Situações específicas (1–8) — solução proposta e regras  
6. `/bash` — especificação completa (SITUAÇÃO 4)  
7. Comandos de gerenciamento (SITUAÇÃO 5) — especificação expandida  
8. Orquestração autônoma: `/plan` + `/run` + guardrails  
9. Observabilidade, segurança e privacidade  
10. Fluxo de desenvolvimento solicitado (análise → implementação → testes → doc)  
11. Artefatos de execução: arquivos `TOOLS_ETAPA_<N>.md` e convenções  
12. Critérios de aceitação e testes automatizados/manual  
13. Instrução final (otimizada) — para o agente executar planos e etapas

---

## 1. Princípios e objetivos
- **Produtividade de Devs**: foco em operações de código, CI, testes, deploy, observabilidade.  
- **Responsabilidade do Sistema**: todo output de tool é exibido **pelo sistema** (UI/terminal), não embutido na resposta do agente. O agente recebe sempre uma cópia estruturada do output.  
- **Autonomia controlada**: o agente pode planejar e executar workflows multi-tool, com guardrails (permissões, custos, aprovação humana).  
- **Reprodutibilidade & Auditabilidade**: cada execução gera um *run manifest* com artefatos, diffs, logs, custos.  
- **Segurança**: sandboxes, listas de bloqueio, redaction automática de segredos, permissões granulares.  
- **Consistência das Tools**: toda tool descreve seu uso, parâmetros, efeitos colaterais, políticas de exibição (`show_cli`) e o sistema implementa a exibição padrão.

---

## 2. Integração com Gemini (LLM)
- **Support multi-model**: Gemini versions (2.0/2.5 Pro/Flash). Possibilidade de trocar por sessão.  
- **Tool Use / Function Calling**: cada tool expõe um schema JSON (nome, parâmetros, required, side_effects, risk_level, show_cli default).  
- **Contexto**: system_instructions limpo (globais), memory (long-term), history (short-term), tool schemas.  
- **JSON Mode**: quando necessário (patches, planos), o LLM deve retornar em formato JSON validável.  
- **Guardrails**: limites de tokens, custo, timeout por etapa. Passos de alto risco pedem `/approve`.  
- **Observability**: every tool call recorded with metadata (start/end, exit_code, bytes_out).

---

## 3. Comandos essenciais (CLI) — lista e comportamento
**Comandos diretos (prioridade inicial):**
- `/help` — lista comandos (sem aliases). Aliases aparecem somente em `/help <comando>`.  
- `/model [nome|info|default <nome>]` — sem parâmetro: lista modelos; com parâmetro: altera o modelo da sessão atual.  
- `/context` — mostra exatamente o que será enviado ao LLM (system, persona, memory, histórico resumido, ferramentas). Visual de token usage por bloco.  
- `/cost` — exibe tokens acumulados, tempo de sessão e custo estimado por modelo.  
- `/export` — exporta contexto e artefatos (txt/md/json/zip). Solicita caminho.  
- `/tools` — lista tools, schemas e permissões necessárias.  
- `/plan <objetivo curta frase>` — solicita ao agente um plano multi-step com tools, critérios de sucesso e rollbacks.  
- `/run` — executa o plano vigente passo-a-passo (autonomia controlada).  
- `/approve [step|all]` — aprova passos marcados de alto risco.  
- `/stop` — interrompe execução autônoma em curso.  
- `/undo` — reverte alterações do último run (via patches/diff).  
- `/diff` — mostra diffs entre estado atual e mudanças propostas.  
- `/patch` — gera patch (diff unificado). `/apply` aplica com validações.  
- `/memory` — `show|set|clear|import|export` (gerenciamento de memória do agente).  
- `/clear` — limpa *histórico de conversa* (mas mantém memory e system) — **se precisar reset completo, usar `/cls reset`**.  
- `/cls reset` — limpa tudo: histórico, memória de sessão, planos, tokens (RESETAR A SESSÃO) — corresponde ao requisito SITUAÇÃO 7.  
- `/compact [instr]` — sumariza histórico em um resumo mantido.  
- `/permissions` — gerencia regras allow/deny por tool/ação/diretório.  
- `/sandbox on|off` — força execução de tools em sandbox.  
- `/logs` — exibe logs estruturados da sessão.  
- `/status` — versão, modelo ativo, conectividade, tools carregadas, permissões.

**Regras UX:**  
- Ao pressionar `/`, mostrar **apenas os comandos** (sem aliases). Aliases são visíveis somente via `/help <comando>` (SITUAÇÃO 8).  
- Mensagens do agente devem ser curtas por padrão; detalhes e JSON em `--detail` ou "expandir".

---

## 4. Tools essenciais — contrato mínimo e padrão de exibição
**Regras gerais para todas as tools**
- **Descrição interna**: cada tool contém sua especificação (o *manual* de uso), incluindo quando usar `show_cli=true|false`.  
- **Visibilidade**: o **sistema** é responsável por exibir o resultado da tool ao usuário quando `show_cli=true`. O agente **não** deve replicar a saída no texto de resposta. (SITUAÇÃO 2 & 3)  
- **Schema** (exemplo mínimo):  
```json
{
  "name": "list_files",
  "params": { "path": "string", "pattern": "string?", "show_cli": "boolean" },
  "returns": { "files": ["string"], "metadata": { "cwd": "string", "count": "int" } },
  "side_effects": "none",
  "risk_level": "low",
  "display_policy": "system"
}
```
- **Artefato**: cada chamada gera artefato `<run_id>_<tool>_<timestamp>.json` com `input`, `output`, `metadata`.  
- **Logs**: tool calls resultam em eventos de log com `actor=tool`, `tool_name`, `run_id`.

**Lista de tools e funções (prioridade)**
1. **/bash** — executar comandos do SO (VER seção 6).  
2. **FS Tool** — `read(path, show_cli)`, `write(path, content)`, `append`, `mkdir`, `rm`, `glob`, `search(pattern)`.  
3. **list_files(path, show_cli)** — retorna árvore/flat list; `show_cli=true` tem responsabilidade de exibição por sistema.  
4. **Editor/Patch Tool** — `generate_patch(file, patch)`, `apply_patch(patch, dry_run)`.  
5. **Git Tool** — `status`, `diff`, `commit(message)`, `branch`, `checkout`, `apply_patch`, `push(gated)`.  
6. **Tests Tool** — `run_tests(args)`, `report_coverage`, `save_report`.  
7. **Lint/Format Tool** — `run_lint`, `auto_fix` (com dry-run).  
8. **Search Tool (repo)** — `find_in_files(query, context_lines=50, max_matches)` — **quando em buscas internas, retornar apenas ~50 lines em torno do trecho** (SITUAÇÃO 6).  
9. **Doc/RAG Tool** — busca em docs locais com embeddings para RAG.  
10. **HTTP Tool** — `request(method, url, headers, body)`.  
11. **Tokenizer/Context Tool** — `estimate_tokens(text)`, `tokenize_for_model(model)`.  
12. **Secrets Tool** — `scan_for_secrets(paths)`, `redact(text)`.  
13. **Process Tool** — `list_jobs`, `kill(pid)`, `attach(pid)`.  
14. **Archive Tool** — `zip(paths)`, `unzip`.  

Cada tool deve documentar: `usage`, `params`, `returns`, `side_effects`, `display_policy`, `examples`, `risk_level`.

---

## 5. Situações específicas (resolução e regras)
### SITUAÇÃO 1 (list_files format)
- **Problema**: caracteres grafados (`├`, `⎿`) em uma única linha causam quebra visual.  
- **Solução**: `list_files` deve retornar lista estruturada (JSON) e o **sistema** formata para exibição substituindo `├` por `\n├` e garantindo quebra antes de `⎿` (ou melhor: renderizar tree com linhas novas). Não confiar em dump textual na instruction.

### SITUAÇÃO 2 (onde listar)
- **Fluxo correto** (obrigatório):  
  1. Usuário pede lista.  
  2. Agente chama `list_files(path, show_cli=true)`.  
  3. **Sistema** exibe a lista formatada no terminal/UI (não a resposta do agente).  
  4. Agente recebe o output formal e responde: “Listei os arquivos; tenho o contexto.”  
- **Quando `show_cli=false`**: agente usa listagem apenas para contexto interno; nada é exibido ao usuário.

### SITUAÇÃO 3 (exibição das tools)
- **Regra global**: sistema sempre **exibe** (print/UX) qual tool está executando e seu resultado (quando `show_cli=true`). O agente recebe a cópia estruturada e age sobre ela. Evitar duplicidade de informações na resposta do agente.

### SITUAÇÃO 6 (find_in_files)
- `find_in_files(query, max_context_lines=50, max_matches=20)` deve retornar: `file`, `line_number`, `match_snippet` (com up to 50 lines total — 25 acima/25 abaixo, ou 50 após como preferido), `match_score`, `path`. Isso economiza tokens.

### SITUAÇÃO 7 (`/cls reset`)
- `/cls` sozinho limpa a tela, mas **não** o histórico.  
- Implementar `/cls reset` ou `/cls --full` que *reseta a sessão*: limpa histórico, limpa memory da sessão (não necessariamente long-term memory persistida), zera tokens. Confirmar com o usuário (padrão: prompt de confirmação).

### SITUAÇÃO 8 (aliases)
- Ao apertar `/`, mostrar **somente comandos**.  
- Exibir aliases no `/help <comando>` (ex.: `/help /bash` lista `/sh`, `/shell` como aliases).

---

## 6. `/bash` — especificação completa (SITUAÇÃO 4)
**Objetivo**: executar comandos do SO, replicar saída ao usuário e fornecer artefato completo ao agente para análise.

### Comportamento esperado
- Input: `/bash <cmd-string>` (pode incluir flags: `--dry-run`, `--cwd`, `--timeout`, `--sandbox`, `--show-cli true|false`).  
- Execução:
  1. Detectar plataforma: `platform.system()` e escolher executor.  
  2. Determinar se precisa de PTY (heurística): se `cmd` contém programas interativos (ex.: `top`, `htop`, `vim`, prompts), usar PTY.  
  3. Executar via PTY quando disponível; fallback para `subprocess.Popen` com pipes.  
  4. **Tee** o output: exibe ao terminal do usuário em tempo real **e** grava em buffer/arquivo (artefato).  
  5. Capturar `stdout`, `stderr`, `exit_code`, `start/end timestamps`, `cwd`, `user_env` (masked), `bytes_out`.  
  6. Redactar segredos detectados no output (usar `Secrets Tool`) — informar se houve redaction.  
  7. Se `show_cli=false`, não exibir output; se `true`, exibir via sistema e, paralelamente, gravar artefato.  
  8. Retornar ao agente: `artifact_id` with link/path, `metadata`, `summary` (pequeno). **Não** incluir dump massivo no prompt; em vez disso, agente pode pedir partes do artifact.

### Segurança e limites
- **Blacklist**: commands proibidos (`rm -rf /`, `poweroff`, `shutdown`, `dd if=... of=...`, `mkfs`, etc). Bloquear por regex e pedir confirmação elevada `/approve`.  
- **Sandbox**: se `/sandbox on`, executar em container (e.g., Docker) com limites de recursos.  
- **Timeout**: default 60s, configurável por flag.  
- **Truncamento**: arquivos/outputs > N MB são truncados; cabeçalho/rodapé mostrados; artefato completo preservado (se permitido).  

### Artefatos
- `<run_id>_bash_<seq>.log` (texto), `<run_id>_bash_<seq>.json` (metadata). Disponíveis para download/export.

### Implementação técnica (esqueleto)
- Unix PTY: `pty.spawn` or `ptyprocess` + `select` loop for reading/writing; duplicate with `tee`.  
- Windows: use ConPTY via `pywinpty`/`conpty` wrappers, fallback to `subprocess`.  
- For TUIs, spawn child PTY, mirror onto parent terminal; agent gets child output buffer.

---

## 7. Comandos de gerenciamento (SITUAÇÃO 5) — detalhados
**/model [nome|info|default <nome>]**  
- Sem args: lista modelos com `name, type, tokens_limit, cost_per_1k`.  
- `info`: retorna JSON detalhado (capabilities, recency, multimodal).  
- `default <nome>`: seta default global.

**/context**  
- Exibe: `system_instructions`, `persona`, `memory (short-summary)`, `history` (pronto para enviar), `tools` (lista com schemas), token count por bloco. Fornece `--export` flag.

**/cost**  
- Mostra tokens totais (prompt+completion), chamadas a tools (tokens), tempo total, custo estimado por modelo e por run.

**/export**  
- Opções: `--format {txt,md,json,zip}`, `--path <path>`. Inclui manifest dos runs.

**Outros comandos**  
- `/plan`, `/run`, `/approve`, `/stop`, `/undo`, `/diff`, `/patch`, `/apply`, `/memory`, `/clear`, `/compact`, `/permissions`, `/sandbox`, `/logs`, `/status` (já especificados na seção 3).

---

## 8. Orquestração autônoma: `/plan` → `/run` (detalhado)
**/plan <objetivo>**
- O agente cria um plano: `[step1, step2, ...]` where each step has:
  - `id`, `tool_name`, `params`, `expected_output` (assert), `rollback`, `risk_level`, `timeout`, `requires_approval` (bool).  
- O system grava o plano in `PLANS/<plan_id>.json` and in `PLANS/PLANS_ETAPA_<N>.md` (human-readable).

**/run**
- Executa steps sequencialmente:
  1. Validar permissões e guardrails (custo estimado, timeout total).  
  2. Para cada step:
     - If `requires_approval` → pause and request `/approve`.  
     - Executar tool; capturar artefato; sistema exibe o resultado (se `show_cli=true`).  
     - Validar `expected_output` (tests/checks); em falha, follow `rollback` or request instruction.  
     - Registrar evento no manifest.  
  3. Ao fim, gerar `post-mortem` (changes applied, artefats, errors, duration, cost).  
- `/stop` interrupts execution; generate partial manifest with status "interrupted".

**Fallbacks and errors**
- Retries with backoff for transient failures (configurable `--retries n`).  
- On irreversible failure, execute `rollback` if defined; otherwise pause and request decision.

---

## 9. Observability, security and privacy
- **Logs estruturados** (JSONL): `timestamp`, `actor` (agent/system/tool), `run_id`, `tool`, `params_hash`, `exit_code`, `artifact_path`.  
- **Redaction** automático de tokens/chaves (Secrets Tool). Registrar se houve redaction.  
- **Permissões**: `/permissions` controla quem/que pode executar ferramentas perigosas (specially `bash`, `git push`, etc).  
- **Opt-in telemetry**: se habilitada, enviar somente métricas agregadas e anonimadas.  
- **Retention**: artefatos sensíveis expirarem (configurável).

---

## 10. Fluxo de desenvolvimento solicitado (passo a passo)
O agente deve seguir rigorosamente o plano abaixo — cada etapa será documentada em arquivo `TOOLS_ETAPA_<N>.md` e executada uma a uma.

**Etapa 0 — Análise inicial (TOOLS_ETAPA_0.md)**  
- Listar todos os arquivos relevantes do projeto (scripts, bin, server, agents, tools, docs, config).  
- Identificar pontos de integração com tools e os módulos que vão mudar.  
- Produzir inventário de risco (lista de ações perigosas) e dependências externas.  
- Entregar `TOOLS_ETAPA_0.md` com inventário e checklist.

**Etapa 1 — Design e contratos (TOOLS_ETAPA_1.md)**  
- Especificar schemas de cada tool (JSON Schema).  
- Definir UI contract para exibição (show_cli behavior).  
- Definir `/bash` design completo com PTY/tee e sandbox.  
- Definir plan manifest schema, run manifest schema e artifact storage.  
- Entregar `TOOLS_ETAPA_1.md` com contratos e exemplos.

**Etapa 2 — Implementação core (TOOLS_ETAPA_2.md)**  
- Implementar infra de tool registry e executor genérico.  
- Implementar `list_files`, `list_files.show_cli` integration and formatting.  
- Implementar `FS Tool`, `Search Tool (find_in_files)` and `Tokenizer Tool`.  
- Implementar `Secrets Tool` redaction.  
- Entregar `TOOLS_ETAPA_2.md` com diffs e patches.

**Etapa 3 — Implementação /bash (TOOLS_ETAPA_3.md)**  
- Implementar execução com PTY/subprocess, tee, artefatos, blacklist, sandbox options.  
- Implementar captura e armazenamento de artifacts.  
- Testes: comandos simples, TUI, blacklisted commands, large outputs (truncate).  
- Entregar `TOOLS_ETAPA_3.md`.

**Etapa 4 — Comandos e Orquestração (TOOLS_ETAPA_4.md)**  
- Implementar `/plan`, `/run`, `/approve`, `/stop`, `/undo`, `/diff`, `/patch`, `/apply`.  
- Integrar ferramentas com plan manifest execution.  
- Entregar `TOOLS_ETAPA_4.md`.

**Etapa 5 — Segurança & Permissões (TOOLS_ETAPA_5.md)**  
- Implementar `/permissions` rules, sandbox enforcement, redaction audit logs.  
- Entregar `TOOLS_ETAPA_5.md`.

**Etapa 6 — UX & CLI polish (TOOLS_ETAPA_6.md)**  
- Implementar help UX (no aliases on `/`), `/help <command>` shows aliases.  
- Implement `/cls reset` full-session reset.  
- Implement `/context` and `/export`.  
- Entregar `TOOLS_ETAPA_6.md`.

**Etapa 7 — Tests, CI and Docs (TOOLS_ETAPA_7.md)**  
- Criar testes automatizados (unit + integration).  
- Criar CI pipeline (GH Actions) to run tests lints, run basic plan runs in sandbox.  
- Revisar e atualizar `docs/2.md`.  
- Entregar `TOOLS_ETAPA_7.md`.

**Etapa 8 — Review & Release (TOOLS_ETAPA_8.md)**  
- Code review, security review, performance review.  
- Packaging, version bump, release notes.  
- Entregar `TOOLS_ETAPA_8.md`.

---

## 11. Artefatos de execução e convenções
- Plan files: `PLANS/PLAN_<timestamp>_<id>.json` and human `PLANS/PLAN_<id>_README.md`.  
- Tool artifacts: `ARTIFACTS/<run_id>/<tool>_<seq>.(json|log|zip)`.  
- Run manifest: `RUNS/RUN_<id>.json` (states: created, running, success, failed, aborted).  
- Tools etapa docs: `TOOLS_ETAPA_<N>.md` — cada uma com checklist, tarefas, arquivos a alterar, diffs e testes.

---

## 12. Critérios de aceitação & testes
**Critérios mínimos**  
- Todas as tools têm JSON schema e `display_policy` implementadas.  
- `/bash` exibe a saída quando `show_cli=true` e grava artefato; PTY funciona em Unix e fallback em Windows.  
- `list_files` retorna JSON e o sistema formata a tree sem linhas quebradas incorretas.  
- `/cls reset` zera sessão (histórico e memória de sessão).  
- `/` mostra apenas comandos; `/help <comando>` mostra aliases.  
- `find_in_files` devolve apenas context_lines ≤ 50 por match.  
- Orquestração `/plan` → `/run` executa steps, registra manifest, e permite `/stop` e `/approve`.

**Testes recomendados**  
- Unit tests para cada tool (inputs/outputs).  
- Integration tests:
  - chamar `/bash` com TUI app (ex.: `python -m http.server` breve).  
  - simulate plan with 3 steps (read file → patch → run tests) in sandbox.  
  - run `list_files` with complex tree and validate UI formatted output (no `├` in single line).  
  - `/cls reset` clears session — assert tokens/history=0.  
- Safety tests: attempt blacklisted commands are blocked and require approval.

---

## 13. Instrução final (otimizada e pronta para ser colocada como `system_instructions` / `planning_instructions` do agente)
> **Observação**: abaixo está a sua instrução original (compactada) seguida da versão otimizada, pensada para máxima clareza e para guiar o agente na criação dos arquivos de planejamento e na execução autônoma, etapa-a-etapa.

### 13.1 Texto original (compactado)
> encontre soluções para as questões abaixo. precisa ser A MELHOR SOLUÇÃO, sempre alinhado com as MELHORES PRÁTICAS DE ARQUITETURA DE SOFTWARE E DESENVOLVIMENTO EM PYTHON.  
> (Incluía SITUAÇÃO 1–3, 4–5, 6–8, e passos de análise/planejamento/execução/documentação).

### 13.2 INSTRUÇÃO FINAL (OTIMIZADA — **USE ISTO COMO A INSTRUÇÃO-MESTRE**)
> **Instrução Mestre (para D.E.I.L.E.)**  
> Você é D.E.I.L.E., um agente de suporte a desenvolvedores integrado ao Gemini. Seu objetivo é **entregar a melhor solução** alinhada às melhores práticas de arquitetura de software e desenvolvimento em Python. Trabalhe com autonomia, porém respeite guardrails, permissões e segurança. Execute o seguinte processo **sem gambiarras**:
> 
> 1. **Análise inicial**  
>    - Liste e identifique todos os arquivos relevantes do repositório (scripts, agents, tools, docs, config). Gere `TOOLS_ETAPA_0.md` com inventário e risco.  
>    - Não altere nada ainda. Apenas **explore** via tools: `list_files`, `read`, `search`. Use `show_cli=true` somente quando for exibir algo ao usuário.
> 
> 2. **Planejamento por etapas**  
>    - Crie um plano detalhado e dividida em arquivos separados: `TOOLS_ETAPA_1.md`, `TOOLS_ETAPA_2.md`, ... Cada `TOOLS_ETAPA_<N>.md` contém: objetivo, arquivos a alterar, schema das tools, exemplo de input/output, checklist de testes e critérios de aceitação.  
>    - O plano deve ser incremental e sempre reversível (inclua `rollback`).  
> 
> 3. **Design de contratos de tools**  
>    - Para cada tool, defina JSON Schema, `display_policy` (`system`), `risk_level`, e `show_cli` default. Documente exemplos de uso e restrições de segurança.  
> 
> 4. **Implementação controlada**  
>    - Execute apenas **uma etapa por vez**: aplique patches gerados em `TOOLS_ETAPA_<N>.md`, rode testes locais (em sandbox quando necessário), verifique resultados. Após confirmar, gere um patch consolidado (ex.: `PATCH_ETAPA_<N>.diff`) e inclua no `RUNS` manifest.  
> 
> 5. **Execução e observabilidade**  
>    - Cada comando/tool call deve gerar artefato gravado em `ARTIFACTS/<run_id>/`. O sistema exibirá outputs quando `show_cli=true`; o agente nunca duplica a mesma saída em sua resposta.  
> 
> 6. **Autonomia segura**  
>    - Para passos com `risk_level >= high`, pause e solicite `/approve`. Não execute pushes ou comandos destrutivos sem aprovação explícita. Use sandbox por padrão se houver risco.  
> 
> 7. **Testes e validação**  
>    - Execute testes automatizados e integrações definidas na etapa. Falhas geram `rollback` ou pausa para intervenção. Agrupe correções e aplique de uma vez após validação.  
> 
> 8. **Documentação final**  
>    - Atualize `docs/2.md` incluindo o novo design das tools, fluxos `/plan`→`/run`, exemplos de runs e política de segurança.  
> 
> 9. **Entrega**  
>    - Gere `RUNS/RUN_<id>.json` com manifest completo, `ARTIFACTS` zipado, `PATCHES` e `TOOLS_ETAPA_<N>.md`. Forneça um `post-mortem` conciso com o que foi alterado, por quê, e próximos passos recomendados.  
> 
> 10. **Regras operacionais importantes**  
>    - **Sistema GUI/CLI é responsável por exibir outputs de tools** (quando `show_cli=true`). O agente **recebe** sempre os artefatos e metadados.  
>    - `list_files` retorna estrutura JSON; o sistema converte para tree legível evitando quebra de linha incorreta (SITUAÇÃO 1).  
>    - `find_in_files` deve retornar ~50 linhas de contexto por match (SITUAÇÃO 6).  
>    - `/cls reset` reseta sessão completamente (SITUAÇÃO 7).  
>    - Ao digitar `/`, mostrar apenas comandos (sem aliases). `/help <comando>` exibe aliases (SITUAÇÃO 8).  
> 
> **Autorização de execução**: ao receber esta instrução, gere `TOOLS_ETAPA_0.md` e aguarde permissão para avançar para ETAPA 1, **ou** se o usuário preferir, inicie automaticamente a ETAPA 1 em sandbox e reporte progresso incremental (cada etapa finalizada deve ser enviada como resumo e o `PATCH` anexado).
