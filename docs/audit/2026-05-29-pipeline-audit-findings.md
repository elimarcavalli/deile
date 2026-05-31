# Auditoria do Pipeline DEILE — 29/mai/2026

> **Auditor**: Opus 4.8 (sessão paralela à correção dos 7 bugs conhecidos pelo Opus #1 em `feat/pipeline-resilience-fixes`).
> **Escopo**: `deile/orchestration/pipeline/`, `deile/orchestration/forge/`, `infra/k8s/{claude_worker_server,worker_server,deploy}.py`, `deile/infrastructure/deile_worker_client.py`.
> **Método**: leitura linear + grep direcionado por padrões de risco (race, except genérico, shell injection, sync I/O em async, leaks de token, comparações de identidade).

## Sumário executivo

Foram identificados **13 achados** novos, não cobertos pela branch `feat/pipeline-resilience-fixes`. Distribuição por severidade:

- 🔴 **3 críticos** (1 bug funcional silencioso no `deploy.py k8s up` para namespaces customizados, 1 mensagem de bloqueio aninhada confusa, 1 sync I/O no event loop do claude-worker server)
- 🟡 **6 médios** (cobre TOCTOU residuais, validação defense-in-depth ausente, env var bugs com erro inútil, label-state mismatch em reaper)
- 🟢 **4 baixos** (DX/observability: doc/code mismatch, mensagens confusas, semantic-zero-handling)

A severidade dominante é **médio**. O código está em bom estado geral — não há SQL/shell injection real, audit logging é tipado, secrets são escondidos por regex bem testada, e a maioria das exceções é capturada de forma intencional (com `noqa: BLE001` justificado em comentário).

A correção do Opus #1 (RESUME_BUDGET, PVC cleanup, HTTP 413, brief autor humano, collector kind=pr, OAuth kubectl, backoff AUTH) **não sobrepõe** nenhum destes achados.

---

## Achados

### 1. 🔴 `deploy.py k8s up` cria namespace customizado com pipe-via-shell que NUNCA EXECUTA

**Categoria**: Correctness
**Arquivo**: `infra/k8s/deploy.py:580-582`

`subprocess.run` recebe um `list` argv contendo `|` como elemento, COM `shell=True`. Quando `shell=True` é passado junto com `args=list`, subprocess interpreta `args[0]` como a *command line* e os demais elementos como `$0..$N` posicionais para o shell — o `|` NÃO é interpretado como pipe.

```python
_run([kubectl, "create", "namespace", ns, "--dry-run=client", "-o", "yaml",
      "|", kubectl, "apply", "-f", "-"],
     shell=True)
```

Verificado empiricamente:
```
$ python3 -c "import subprocess; subprocess.run(['echo','hello','|','cat'], shell=True)"
(saída em branco)
```

**Cenário de reprodução**: operador roda `python3 infra/k8s/deploy.py -n deile-staging k8s up` para criar um stack novo. O comando reporta sucesso, mas o namespace **não é criado** — qualquer `kubectl -n deile-staging apply` subsequente cai em `default` (silenciosamente!) ou falha com `namespace not found`, dependendo da versão do kubectl. Esse é justamente o caminho de poluição do `default` mencionado no CLAUDE.md ("Hard rule: every `kubectl` command MUST carry `-n <ns>`").

**Fix sugerido**: substituir por `subprocess.run(..., shell=True, input=...)` recebendo o YAML via stdin, OU fazer duas chamadas argv (gerar YAML via `_capture` + aplicar via `_run([..., "-f", "-"], input=yaml)`). Removeu-se a possibilidade de `shell=True` + `list` ambíguo.

---

### 2. 🔴 Mensagem de bloqueio aninhada para `WORKER_AUTH_EXPIRED`

**Categoria**: DX / Observability
**Arquivo**: `deile/orchestration/pipeline/stages.py:1594` (chama `_block_issue` com `AUTH_EXPIRED_BLOCK_MSG`)

`AUTH_EXPIRED_BLOCK_MSG` (linhas 1707-1717) já é um comentário formatado *completo*, com seu próprio `⛔`, sua própria instrução em fence ```bash```, e seu próprio call-to-action. Mas `_block_issue` (linha 1768) tomou-o como `reason` cru e o re-embebeu dentro de outro template:

```
⛔ **Pipeline bloqueou esta issue** (`~workflow:bloqueada`).

**Motivo:** ⛔ claude-worker reportou OAuth token expirado/inválido
(`WORKER_AUTH_EXPIRED`). Não vou retentar — token só pode ser renovado via host.

**Como destravar (1 comando):**
```bash
python3 infra/k8s/deploy.py k8s claude-renew
```

Depois remova esta label `~workflow:bloqueada` para o pipeline tentar de novo.


O trabalho parcial foi preservado na branch. Para retomar, remova o
label `~workflow:bloqueada` — o pipeline volta a retomar a implementação
de onde parou.
```

O resultado tem dois `⛔`, duas instruções de "como destravar" parcialmente conflitantes ("renove o token" vs "remova o label"), e o block fence ```bash``` no meio fica visualmente confuso na renderização GitHub.

**Cenário de reprodução**: deixe o token claude expirar e dispare um implement → o comment na issue tem dupla formatação.

**Fix sugerido**: introduzir variante `_block_issue_raw(monitor, number, comment_text)` que aceita o texto FINAL (sem re-embedding); usar para o caminho AUTH_EXPIRED. Mantém o template padrão para reasons que são strings curtas reais.

---

### 3. 🔴 `_save_session_meta` bloqueia o event loop do claude-worker em PVC I/O

**Categoria**: Performance
**Arquivo**: `infra/k8s/claude_worker_server.py:682-697`, chamado em `1504, 1528, 1549, 1595, 1773` etc. dentro de `dispatch_handler` (async)

`_save_session_meta` faz `path.parent.mkdir`, `tmp.write_text(json.dumps(...))`, `os.replace(tmp, path)` — três syscalls bloqueantes — sem `asyncio.to_thread`. É chamado pelo menos 3× por dispatch (pre-spawn, post-cmd-set, post-result). Sob carga (3+ pods claude-worker concorrentes na mesma PVC RWO), latência de fsync no PVC pode ficar perceptível (50-200ms por chamada × 3 = ~0.5s/dispatch travando outros handlers HTTP do mesmo pod, incluindo `health`, `pod-status`, `progress` polling pelo painel).

O lease já está corretamente envelopado em `asyncio.to_thread` (linhas 326, 339, 371). Esse mesmo cuidado faltou ao persistir o `session.json`.

Mesmo problema em `infra/k8s/claude_worker_server.py:622-623` — `stdout_path.write_text(stdout); stderr_path.write_text(stderr)` dentro de `run_subprocess_with_progress`. Tail de 50KiB+10KiB ao PVC bloqueia o loop por ~10ms cada (pode ser mais sob fsync IO press).

**Cenário de reprodução**: dispare 3 dispatches simultâneos via `_acquire_lease` (workspaces diferentes), com PVC sob carga. `GET /v1/health` pode falhar a probe em pico, pod marca-se NotReady, removido do Service, dispatch subsequente recebe 503 do Service.

**Fix sugerido**: envolver as 3 chamadas (`_save_session_meta` e os 2 `.write_text` em `run_subprocess_with_progress`) em `await asyncio.to_thread(...)`. Trivial.

---

### 4. 🟡 `has_bot_activity_since` interpola `bot_login` direto no filtro jq — defense-in-depth ausente

**Categoria**: Security
**Arquivo**: `deile/orchestration/forge/github_forge.py:740, 751`

Em `_has_bot_activity_impl`, `bot_login` é interpolado num filtro `gh api -q '... select(.user.login=="{bot_login}") ...'`. As outras superfícies que aceitam login (linhas 345, 515) validam contra `_GH_LOGIN_RE.fullmatch(login)` explicitamente; esta função **não** valida.

Hoje, o único caller (`_resolve_bot_login` em `stages.py:2122-2129`) hardcoda `"deile-one"`, então é teoricamente seguro. Mas:
- a função é `public` em `ForgeClient` (assinatura compartilhada)
- futuro caller que ler `bot_login` de config/env pode introduzir injection
- o invariante "todos os logins na camada forge são validados antes de tocar jq" é quebrado

**Cenário de reprodução** (hipotético): operador habilita identidade de bot configurável via env, defina `DEILE_BOT_LOGIN='deile-one") | true | select(true)'`. O filtro jq vira sintaticamente válido e retorna sempre true → audit do proof-of-work do reaper sempre confirma "atividade humana" → reaper jamais bloqueia.

**Fix sugerido**: adicionar `if not _GH_LOGIN_RE.fullmatch(bot_login or ""): return False` no início de `_has_bot_activity_impl`.

---

### 5. 🟡 `resolve_stage_timeout_s` / `resolve_stage_max_retries` perdem contexto na mensagem de erro

**Categoria**: DX
**Arquivo**: `deile/orchestration/pipeline/dispatch_resolver.py:217-223, 266-274`

```python
try:
    v = int(raw_env.strip())
    if v <= 0:
        raise ValueError(f"timeout must be > 0, got {v}")
    return v
except ValueError:
    raise
```

Quando `raw_env = "abc"`, `int()` raise com mensagem `"invalid literal for int() with base 10: 'abc'"` — sem dizer QUAL env var nem qual stage. O operador vê só "ValueError: invalid literal..." no log, sem rastreabilidade. O `except ValueError: raise` é cosmético (não acrescenta nada).

**Cenário de reprodução**: operador set `DEILE_PIPELINE_TIMEOUT_S_PR_REVIEW=foo`. Pipeline crash com mensagem genérica de Python.

**Fix sugerido**:
```python
try:
    v = int(raw_env.strip())
    if v <= 0:
        raise ValueError(f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()} must be > 0, got {v!r}")
    return v
except ValueError as exc:
    raise ValueError(f"invalid DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}={raw_env!r}: {exc}") from exc
```

---

### 6. 🟡 `max_parallel=0` é tratado como 1 (silent slot)

**Categoria**: Correctness
**Arquivo**: `deile/orchestration/pipeline/stages.py:1357`

```python
available_slots = max(0, max(1, monitor.config.max_parallel) - in_flight)
```

A intenção do `max(1, ...)` é proteger contra `max_parallel=-1` por config corrupta. Mas o efeito colateral é que `max_parallel=0` (operador querendo "paralelismo desligado, modo serial estrito") vira efetivamente `max_parallel=1`.

**Cenário de reprodução**: operador set `DEILE_PIPELINE_MAX_PARALLEL=0` para freezar implementações temporariamente sem desligar a stage. Pipeline ainda dispatch 1 issue por tick.

**Fix sugerido**: `max(0, monitor.config.max_parallel - in_flight)` — respeita 0. Se quiser proteção contra negativo, valida no `Settings`.

---

### 7. 🟡 Reaper aplica `to_label` mesmo quando `remove_labels(from_label)` falhou

**Categoria**: Correctness
**Arquivo**: `deile/orchestration/pipeline/stages.py:2478-2488`

```python
try:
    await monitor.forge.remove_labels(kind, number, to_remove)
except GhCommandError as exc:
    logger.warning("reaper #%d: remove_labels failed: %s", number, exc)
try:
    await monitor.forge.add_labels(
        kind, number, [to_label, make_attempt_label(next_attempt)],
    )
```

Se `remove_labels` falhou (rate-limit, network blip, label race), a issue ainda tem `from_label` (e.g. `~workflow:em_implementacao`). Em seguida, `add_labels` aplica `to_label` (`~workflow:revisada`). Resultado: issue com **duas** labels `~workflow:*` simultaneamente — violação do invariante "uma issue carrega exatamente um `~workflow:` state" (princípio explícito em vários comments).

Isso confunde o stage handler do próximo tick (a query `list_issues_with_label(WORKFLOW_REVIEWED)` retorna a issue, mas o re-pickup tenta `transition_issue(from=WORKFLOW_REVIEWED, to=WORKFLOW_IMPLEMENTING)` que pode falhar de novo).

**Cenário de reprodução**: GitHub API rate-limit em pico, `remove_labels` falha com 403, `add_labels` ainda passa. Próximo tick vê issue com `~workflow:em_implementacao` + `~workflow:revisada`.

**Fix sugerido**: `if remove_labels falhou: return` (skip o reap; tenta de novo no próximo tick). Mais conservador que duplicar labels.

---

### 8. 🟡 `_critique_one_issue` aplica ownership label SEM try/except antes da transição

**Categoria**: Correctness
**Arquivo**: `deile/orchestration/pipeline/stages.py:868-873`

```python
await monitor.forge.add_labels("issue", number, [monitor.identity.ownership_label()])
await monitor.notifier.issue_picked_up(number, target.title, target.url)
try:
    await monitor.forge.transition_issue(
        number, from_label=WORKFLOW_NEW, to_label=WORKFLOW_REVIEWING
    )
except GhCommandError as exc:
    await _record_forge_error(monitor, f"could not claim issue #{number} for critique", exc)
    return
```

Se `add_labels` (ownership) falhar com `GhCommandError` (transient), a exceção escapa fora do try/except — derruba o tick inteiro (capturado só no `_run_forever` raiz). Pior: a notificação `issue_picked_up` já foi enviada ANTES da transição, então o operador vê "issue X foi pega" mesmo se a transição falhou.

Compare com `review_one_new_issue` (linha 805-810) que faz `claim_with_batch` → `add_labels(ownership)` → `notifier.issue_picked_up` → `transition_issue` num try-grande — também vulnerável, mas pelo menos o tick não cai.

**Fix sugerido**: envolver `add_labels(ownership)` num try/except local + rollback do batch claim se aplicável. Ou pelo menos catch `GhCommandError` simétrico ao `transition_issue` abaixo.

---

### 9. 🟡 `_count_in_flight_issues` e `implement_one_reviewed_issue` usam critérios divergentes de ownership

**Categoria**: Correctness
**Arquivo**: `deile/orchestration/pipeline/stages.py:1252-1258` vs `1376-1377`

`_count_in_flight_issues` filtra com:
```python
if monitor._this_monitor_owns(i) or ownership_label in i.labels:
    count += 1
```

(OR — qualquer um dos dois)

`implement_one_reviewed_issue` filtra com:
```python
and monitor._this_monitor_owns(i)
and (i.batch_id is not None or ownership_label in i.labels)
```

(AND — ambos)

Isso significa que `_count_in_flight_issues` pode contar issues que `implement_one_reviewed_issue` jamais alcançaria (issue com ownership_label mas com batch_id=None E `_this_monitor_owns=False` — possível em sharded deployments quando o owner migrou).

Resultado: in_flight é superestimado → available_slots é subestimado → menos dispatches do que deveria.

**Cenário de reprodução**: sharded deployment com `monitor_id=A` herda issue rotulada pelo `monitor_id=B` (label `~by:B`). `_this_monitor_owns` (hash do título) coloca em A. `_count_in_flight_issues` conta a issue como "in-flight de A" (porque tem `~by:B` ≠ A, mas o OR aceita `_this_monitor_owns(A)=True`). `implement_one_reviewed_issue` não pickup (precisa `~by:A` ou batch_id). Issue fica idle ocupando slot fantasma.

**Fix sugerido**: unificar predicate em uma função helper `_monitor_can_handle(monitor, issue)`.

---

### 10. 🟢 `_handle_review_concluded_invalidation` pretende "freshly invalidated PR can be picked up this tick" mas snapshot é stale

**Categoria**: DX / Doc-Code mismatch
**Arquivo**: `deile/orchestration/pipeline/stages.py:1929-1940`

Comentário diz:
> Runs BEFORE candidate selection so a freshly invalidated PR can be picked up this tick.

Mas o `_candidate(pr)` na linha 1942 itera a MESMA `prs` list (snapshot de linha 1910, antes da invalidation). `pr.labels` ainda contém `REVIEW_CONCLUDED` no objeto in-memory; o filter de candidate exclui PRs com `REVIEW_CONCLUDED` (linha 1943) — logo, a PR invalidada **não é claimable no mesmo tick**.

Não é um bug de runtime (no próximo tick ela é claimable), mas o comentário promete algo que o código não entrega.

**Fix sugerido**: re-fetch ou mutate `pr.labels` em memória após `_handle_review_concluded_invalidation` retornar; OU corrigir o comentário para "será picked up no próximo tick".

---

### 11. 🟢 `DispatchLedger._cache` nunca expira — múltiplas instâncias ficam dessincronizadas

**Categoria**: Correctness (defensiva)
**Arquivo**: `deile/orchestration/pipeline/dispatch_ledger.py:85-106`

O cache é populado no primeiro `_load()` e só invalidado via `invalidate_cache()` (manual, usado em testes). Se houver mais de uma instância de `DispatchLedger` apontando para o mesmo path (e.g. status server tem sua própria + monitor tem a sua), ambas verão snapshots divergentes — escrita numa não é vista pela outra até `invalidate_cache()`.

Hoje o design "single instance, single writer" é mantido por convenção (singleton no `monitor`), mas a tipagem não previne plural — o painel poderia, no futuro, criar uma instância para ler `list_all()` sem saber que precisa invalidar.

**Cenário de reprodução**: futuro endpoint `/v1/pipeline-status/ledger` instancia `DispatchLedger()` localmente em vez de receber o singleton — vê dados stale para sempre.

**Fix sugerido**: mtime check no `_load` (se file mtime > cache time, reload); ou documentar explicitamente o invariante "única instância por processo, sem garantias multi-process".

---

### 12. 🟢 `_TASK_LOCK` no worker_server serializa TODAS as tasks — `max_parallel` no pipeline é ineficaz para deile-worker

**Categoria**: Performance
**Arquivo**: `infra/k8s/worker_server.py:132, 616`

```python
_TASK_LOCK = asyncio.Lock()  # MVP: serialize CWD-coupled work
...
async with _TASK_LOCK:
    prev_cwd = os.getcwd()
    try:
        os.chdir(workdir)
```

O comentário "MVP: serialize CWD-coupled work" é honesto, mas o efeito é que `max_parallel=2` no pipeline + 1 réplica `deile-worker` = 1 task efetiva em vôo. O ganho de paralelismo só aparece com 2+ réplicas de worker. Tudo bem operacionalmente (CLAUDE.md menciona "needs >=2 worker replicas to actually run in parallel"), mas o `_TASK_LOCK` torna O `max_parallel` no pipeline um upper bound INALCANÇÁVEL em deployment default — pode confundir o operador.

Não é um bug, é uma DX gap: o status `available_slots = 2` no log do pipeline sugere que 2 tasks rodam concurrentes, quando na verdade são sempre sequenciais por replica.

**Fix sugerido (longo prazo)**: substituir `os.chdir` por `subprocess.Popen(cwd=workdir)` ou por uma extracted shell, eliminando o `_TASK_LOCK`. Ou simplesmente documentar mais alto que paralelismo só vale com `--worker N`.

---

### 13. 🟢 `sessions_command_handler` expõe `full_prompt` cru (pode conter conteúdo privado da issue)

**Categoria**: Security
**Arquivo**: `infra/k8s/claude_worker_server.py:2109-2117`

```python
return web.json_response({
    "task_id": task_id,
    "cmd": meta.get("command") or [],
    "full_prompt": meta.get("full_prompt") or "",
    ...
})
```

O `full_prompt` contém o issue body + preamble do stage. Issue body é "informação pública" para repos open-source, mas:
- Para repos privados (modelo enterprise / monorepo de cliente), o body pode conter PII, credenciais cited, ou business-sensitive context
- O endpoint está atrás de Bearer auth, OK — mas qualquer pessoa com acesso ao painel TUI tem acesso ao prompt (que é uma escalada para repos onde só algumas pessoas devem ver issues protegidas)

O `env_redacted` é tratado com cuidado (linha 2117), mas o prompt em si não passa por `SecretsScanner.redact_text` antes de sair.

**Cenário de reprodução**: usuário com acesso ao painel mas SEM acesso à issue privada pode ler o body via `GET /v1/sessions/<id>/command`.

**Fix sugerido**: rodar `full_prompt` por `SecretsScanner.redact_text` antes de retornar; OU adicionar segundo nível de auth (apenas admins) para esse endpoint específico; OU adicionar parameter `?redacted=true` (default) e require explicit `?raw=true` com audit log.

---

## Findings já em correção (NÃO duplicar)

O Opus #1 está trabalhando em:

- `RESUME_BUDGET` — handler 413 quando session JSONL > threshold
- PVC cleanup — workspaces órfãos no `/home/claude/work/`
- HTTP 413 handling no client side (pipeline → fallback fresh)
- Brief de "autor humano" (mention/comment rico)
- Collector com `kind=pr` (refactor "PR é o quadro" — PR #411)
- OAuth kubectl renew (relação com claude-worker bootstrap)
- Backoff exponencial em AUTH errors

Nenhum dos achados desta auditoria sobrepõe os 7 acima.

---

## Não-achados (categorias verificadas, OK)

Categorias varridas que não revelaram problemas:

- **Shell injection direto via `subprocess`**: todos os `subprocess.run`/`create_subprocess_exec` usam argv (lista) com argumentos validados. O único `shell=True` encontrado é o bug #1 (que é por outra razão).
- **`pickle.load` / `eval`**: zero ocorrências no escopo auditado.
- **Token leak em log**: `secrets_scanner.redact_text` cobre rotacionalmente; bearer no header `Authorization` nunca é logado (`extra={"request_id": ..., "wait": ...}` em `deile_worker_client.py:543`).
- **`==` vs `is None`**: amostragem nos 10K+ linhas — uso correto de `is None` / `is not None`.
- **`bare except`**: zero ocorrências (todas são `except Exception` com `noqa: BLE001` justificado).
- **`asyncio.CancelledError` mal-tratada**: única ocorrência é `worker_server.py:1019` que correctly re-raises após gravar state terminal.
- **Loops sem cap**: todos os loops sobre listas vindas da forge têm `limit=` explícito (50, 100, 200 conforme contexto); progress polling tem `_POLL_TIMEOUT_S=5.0` cap.
- **`claim_with_batch` TOCTOU**: tem janela residual entre `current.batch_id is None` (linha 547) e `add_labels` (linha 553), mas o re-fetch + foreign-label check (linha 558-571) é defesa correta. Pior caso é dois ticks perdidos, não dispatch duplicado.
- **`_acquire_lease` race entre pods**: protocol write-tmp+rename + re-read confirm é POSIX-correto. Last-rename-wins é benigno (perdedor retorna None, vencedor segue).
- **Authentication bypass no pipeline-status-server**: `hmac.compare_digest` + fail-loud no startup se token ausente (`RuntimeError`). Correto.
- **Path traversal em `task_id`**: `_TASK_ID_RE` fullmatch antes de tocar filesystem nos endpoints `/v1/sessions/*`. Correto.
- **CSP / CORS no pipeline-status-server**: irrelevante (consumido só pelo painel via aiohttp, sem browser).
- **Reentrância em `process_mentions`**: idempotência via `~mention:processado` label; cursor persistido em `_mention_cursor_path` (sync I/O dentro de async, ver achado #3 análogo — mas é monitor-process, não worker pod, então menos crítico).

---

## Recomendação de priorização (para o operador)

Se for fazer 3 fixes nesta semana:
1. **#1** (deploy.py pipe) — alto risco operacional + trivial de corrigir
2. **#3** (sync I/O no claude-worker) — toca pod readiness sob carga + 5 linhas de fix
3. **#7** (reaper aplica `to_label` após falha de remove) — corrupção silenciosa de estado de label que polui pipeline

Os outros são melhores em janela de manutenção (não bloqueiam, mas degradam DX/segurança defensiva).
