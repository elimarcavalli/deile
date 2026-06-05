# 15 — Pipeline Logger (logging canônico do ciclo do pipeline)

> Helper de logging estruturado para o pod `deile-pipeline`. Emite eventos tipados ao logger `deile.pipeline.events` com 15 funções públicas, garantias formais de isolamento de falhas, deduplicação e sanitização.

## 1. Propósito

`deile/orchestration/pipeline/pipeline_logger.py` é o único ponto de emissão de logs de ciclo do pipeline: refinamento, decomposição, batch, label, reaper, autenticação e roteamento. Centralizar as emissões garante formato uniforme, sanitização consistente e rastreabilidade end-to-end sem espalhar `logging.getLogger(...)` pelos estágios.

## 2. Chamadores diretos

| Arquivo | Ponto de importação | O que importa |
|---|---|---|
| `deile/orchestration/pipeline/stages.py` | linha 32 | `import pipeline_logger` (namespace) + importações nomeadas de `log_decomposition_fanout`, `log_reaper_block`, `log_reaper_unblock`, `log_refinement_critique`, `log_refinement_refine`, `log_routing_mention`, `log_routing_pr_unified`, `log_routing_dropped` |
| `deile/orchestration/pipeline/monitor.py` | linha 53 | `import pipeline_logger` (namespace) + importações nomeadas de `log_auth_backoff`, `log_auth_fail`, `log_auth_recover`, `log_auth_skip` |

`monitor.py` registra o callback de mudança de label no construtor de `PipelineMonitor` (linha ≈ 363):

```python
self.forge.on_label_change = lambda kind, num, rem, add: \
    pipeline_logger.log_label_change(target_kind=kind, target=num, removed=rem, added=add)
```

## 3. Ciclo de vida dos eventos

| Família | Subfunção | Chamador | Momento no ciclo |
|---|---|---|---|
| `refinement.critique` | `log_refinement_critique` | `stages.py` | Após persona de crítica avaliar a issue; antes de mutar labels de workflow |
| `refinement.refine` | `log_refinement_refine` | `stages.py` | Após persona de refinamento reformular o body |
| `decomposition.fanout` | `log_decomposition_fanout` | `stages.py` | Após decomposição da intent em sub-issues derivadas |
| `batch.claim` | `log_batch_claim` | `stages.py` | **Antes** de mutar labels da issue (marcação `~batch:sha`) |
| `batch.release` | `log_batch_release` | `stages.py` | Ao liberar o lote (após completar ou cancelar) |
| `label.change` | `log_label_change` | `monitor.py` (via `on_label_change`) | **Após** alteração de label concluída em `monitor.py:363` |
| `reaper.unblock` | `log_reaper_unblock` | `stages.py` | Quando reaper decide desbloquear item travado |
| `reaper.block` | `log_reaper_block` | `stages.py` | Quando reaper decide bloquear item que excedeu tentativas |
| `auth.fail` | `log_auth_fail` | `monitor.py` | Após falha de autenticação com target |
| `auth.backoff` | `log_auth_backoff` | `monitor.py` | Ao iniciar período de backoff exponencial |
| `auth.skip` | `log_auth_skip` | `monitor.py` | Quando target está em pausa de backoff e o tick o ignora |
| `auth.recover` | `log_auth_recover` | `monitor.py` | Quando target volta a autenticar com sucesso após falhas |
| `routing.mention` | `log_routing_mention` | `stages.py` | Ao rotear menção/atribuição de uma issue ou PR |
| `routing.pr_unified` | `log_routing_pr_unified` | `stages.py` | Ao rotear PR para o brief unificado de revisão |
| `routing.dropped` | `log_routing_dropped` | `stages.py` | Quando evento é descartado por regra de roteamento |

## 4. Formato canônico

```
familia.subtipo  k1=v1 k2='v com espaço' k3=42 k4=[el1,el2]
```

| Regra | Detalhe |
|---|---|
| Logger name | `deile.pipeline.events` |
| Separador família/campos | dois espaços (`  `) |
| Strings sem espaço | sem aspas (`sha=abc123`) |
| Strings com espaço | aspas simples (`reason='batch scheduled'`) |
| Inteiros e booleans | sem aspas (`attempts=3`) |
| Listas | `[el1,el2]` — sem espaços após vírgula (via `_fmt_value`) |
| Comprimento máximo | 500 chars — linha truncada em `_MAX_LINE` por `_build_line` |

### Exemplos reproduzíveis

**Batch claim com campo lista:**

```python
log_batch_claim(sha="d4e5f6", issues=[101, 102, 103], reason="batch scheduled")
```

Linha emitida:
```
batch.claim  sha=d4e5f6 issues=[101,102,103] reason='batch scheduled'
```

**Label change com dois campos lista:**

```python
log_label_change(
    target_kind="issue", target=42,
    removed=["~workflow:nova"],
    added=["~workflow:implementando"],
)
```

Linha emitida:
```
label.change  target_kind=issue target=42 removed=[~workflow:nova] added=[~workflow:implementando]
```

**Critique sem gaps (campo opcional omitido):**

```python
log_refinement_critique(issue=581, round=1, persona="critic", verdict="approved")
```

Linha emitida:
```
refinement.critique  issue=581 round=1 persona=critic verdict=approved
```

## 5. API pública completa

> Verificação mecânica: `grep "^def log_" deile/orchestration/pipeline/pipeline_logger.py` deve retornar exatamente as 15 funções abaixo.

Todas as funções são **keyword-only** (parâmetros precedidos por `*`). Nenhuma propaga exceção (ver garantia **never-raises**).

### 5.1 Família `refinement`

```python
def log_refinement_critique(
    *, issue: int, round: int, persona: str, verdict: str, gaps: str = ""
) -> None
```

Emite `refinement.critique`. Campo `gaps` é opcional; quando presente, é sanitizado e truncado em 200 chars.

---

```python
def log_refinement_refine(
    *, issue: int, round: int, persona: str, body_chars: int, verdict: str
) -> None
```

Emite `refinement.refine`. `body_chars` é o tamanho do body refinado (inteiro, sem truncagem por campo).

### 5.2 Família `decomposition`

```python
def log_decomposition_fanout(
    *, intent: int, derivadas: list[int], complexity: list[str]
) -> None
```

Emite `decomposition.fanout`. `derivadas` e `complexity` são listas — formatadas como `[el1,el2]`.

### 5.3 Família `batch`

```python
def log_batch_claim(*, sha: str, issues: list[int], reason: str) -> None
```

Emite `batch.claim`. `issues` é lista de inteiros. `reason` é sanitizado e truncado em 200 chars.

---

```python
def log_batch_release(*, sha: str, reason: str) -> None
```

Emite `batch.release`. `reason` é sanitizado e truncado em 200 chars.

### 5.4 Família `label`

```python
def log_label_change(
    *, target_kind: str, target: int, removed: list[str], added: list[str]
) -> None
```

Emite `label.change`. **Dedup TTL = 30 s** — chave `(target_kind, target, frozenset(removed), frozenset(added))`.

### 5.5 Família `reaper`

```python
def log_reaper_unblock(
    *,
    target_kind: str,
    target: int,
    attempts: int,
    reason: str,
    last_activity_s: int | None = None,
) -> None
```

Emite `reaper.unblock`. **Dedup TTL = 60 s** — chave `(target_kind, target, attempts)`. `last_activity_s` é opcional. `reason` é sanitizado e truncado.

---

```python
def log_reaper_block(
    *, target_kind: str, target: int, attempts: int, cap: int, reason: str
) -> None
```

Emite `reaper.block`. **Dedup TTL = 60 s**. Severity = **WARNING**. `reason` é sanitizado e truncado.

### 5.6 Família `auth`

```python
def log_auth_fail(
    *, target: str, attempts: int, threshold: int, reason: str
) -> None
```

Emite `auth.fail`. **Dedup TTL = 60 s** — chave `("auth.fail", target)`. Severity = **WARNING**. `reason` é sanitizado e truncado.

---

```python
def log_auth_backoff(
    *, target: str, attempts: int, until_iso: str, backoff_s: int
) -> None
```

Emite `auth.backoff`. Severity = **WARNING**. Sem dedup.

---

```python
def log_auth_skip(*, target: str, until_iso: str, remaining_s: int) -> None
```

Emite `auth.skip`. Severity = INFO. Sem dedup.

---

```python
def log_auth_recover(*, target: str, reason: str) -> None
```

Emite `auth.recover`. Severity = INFO. `reason` é sanitizado e truncado.

### 5.7 Família `routing`

```python
def log_routing_mention(*, target_kind: str, target: int, action: str) -> None
```

Emite `routing.mention`. Severity = INFO. Sem dedup.

---

```python
def log_routing_pr_unified(*, target: int, role: str, mode: str) -> None
```

Emite `routing.pr_unified`. Severity = INFO. Sem dedup.

---

```python
def log_routing_dropped(*, target_kind: str, target: int, reason: str) -> None
```

Emite `routing.dropped`. Severity = INFO. `reason` é sanitizado e truncado.

## 6. Garantias formais

### 6.1 Never-raises

Cada função envolve toda a lógica em duplo `try/except`: o bloco externo captura erros de formatação/emissão; o interno captura erros ao tentar emitir o log de debug de fallback. Nenhuma exceção propaga ao call-site.

Arquivo de teste: `test_pipeline_logger_never_raises.py`

### 6.2 No-secrets

Três mecanismos combinados antes de qualquer emissão:

| Mecanismo | Escopo | Implementação |
|---|---|---|
| `_sanitize` | Todos os campos string | Strip de `\n`, `\r`, `\t`; substituição de `'` por espaço |
| `_truncate(value, 200)` | Campos de texto livre: `reason` e `gaps` | Limita a 200 chars e adiciona `...` se excedido |
| `_MAX_LINE = 500` | Linha completa | `_build_line` trunca a linha final se > 500 chars |

Campos escalares (inteiros, `sha`, `verdict`, etc.) não são truncados por campo — apenas pela linha total.

Arquivo de teste: `test_pipeline_logger_no_secrets.py`

### 6.3 Dedup

`_DedupCache` (singleton `_DEDUP` module-level) controla emissões repetidas:

| Parâmetro | Valor |
|---|---|
| Capacidade máxima | 2 048 chaves (`_DEDUP_MAX_KEYS`) |
| Evicção | Primeiro expira as entradas com TTL vencido; se ainda acima do cap, remove as mais antigas |
| Estado | Em memória; perdido no restart (aceitável — dedup é best-effort) |

Funções com dedup ativo:

| Função | TTL |
|---|---|
| `log_label_change` | 30 s |
| `log_reaper_unblock` | 60 s |
| `log_reaper_block` | 60 s |
| `log_auth_fail` | 60 s |

Arquivo de teste: `test_pipeline_logger_dedup.py`

### 6.4 Severity

| Nível | Funções |
|---|---|
| `INFO` (`_LOG.info`) | Todas exceto as listadas abaixo |
| `WARNING` (`_LOG.warning`) | `log_reaper_block`, `log_auth_fail`, `log_auth_backoff` |

Arquivo de teste: `test_pipeline_logger_severity.py`

## 7. Manutenção

Qualquer PR que altere `deile/orchestration/pipeline/pipeline_logger.py` deve atualizar este documento:

- **Nova função pública**: adicionar entrada na seção 5 (assinatura completa + campos keyword-only + dedup/severity se aplicável) e linha na tabela da seção 3.
- **Alteração de TTL ou severity**: atualizar tabelas das seções 6.3 e 6.4.
- **Novo campo ou mudança de truncagem**: atualizar seção 6.2.
- **Mudança de formato** (`_build_line`, `_fmt_value`): atualizar seção 4 e exemplos.

Verificação pós-mudança: `grep "^def log_" deile/orchestration/pipeline/pipeline_logger.py` deve casar 1-a-1 com a lista da seção 5.
