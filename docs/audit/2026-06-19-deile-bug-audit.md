# Auditoria de Bugs DEILE — 2026-06-19 — Issue #776

> **Auditor**: Claude Sonnet 4.6 (claude-worker DEILE, workflow multi-agente paralelo)
> **Escopo**: `deile/core/`, `deile/core/models/`, `deile/orchestration/pipeline/`, `deile/orchestration/forge/`, `deile/tools/`, `deile/security/`, `deile/storage/`, `infra/k8s/{cli_worker_server,claude_worker_server,worker_server,deploy}.py`, `deile/infrastructure/`
> **SHA auditado**: `23427a9` (`fix(pipeline): remover fallback de parse_decompose_result + completar brief de refino`; HEAD de `origin/main` em 2026-06-19)
> **Método**: Varredura paralela por 6 subagentes de scanning independentes (deile/core/models/, deile/core/runtime, forge/gitlab, pipeline/new-files, infra/cli+storage, subagents/orchestration), seguida de verificação adversarial de liveness e dedup contra dois audits anteriores.

---

## Sumário executivo

Foram identificados **29 achados** novos, confirmados vivos em `23427a9` e não cobertos pelos audits anteriores. Distribuição por severidade:

- 🔴 **8 críticos** (timeouts sem guarda no health-check do router, subprocessos forge e git sem timeout, erro-envelope ausente no bot streaming, state-leak de histórico na validation gate, deleção de log sem contabilização no harvester CLI, tick duplicado via force-tick, loop de workflow travado em erro de infra, sync I/O bloqueante no cost_estimator)
- 🟡 **11 médios** (sync I/O em async em context_manager ×2, TOCTOU no branch_exists do gitlab, comparação lexicográfica de timestamps ISO, classificação errada de "refs" como "closes", subprocess orphan leak no implementer, TOCTOU no get_session_store, status de agente sem lock, total_steps sempre zero no workflow_executor, sync I/O no mention cursor do monitor, TOCTOU no branch_exists de duas chamadas)
- 🟢 **10 baixos** (span GeminiProvider com __exit__(None,None,None), timeout_seconds nunca consumido, dead code TOOL_INVOKED no streaming, singleton sem lock no agent.py get_agent(), singleton sem lock no debug_logger, DeprecationWarning asyncio.get_event_loop() ×2, anotação Iterator errada em EmbeddingStore, docstring subprocess.run vs asyncio em implementer, concorrência sem lock em _request_count/_status, singleton _PLAIN_CONSOLE sem lock)

A auditoria revelou **8 bugs críticos** com potencial de travar o pipeline em caminhos comuns: três ausências de timeout em operações de rede (health-check, forge CLI, git), dois casos de estado corrompido silenciosamente (validation gate history leak, workflow loop sem marcação de FAILED), dois bugs de resultado errado no bot streaming (error envelope vazio, tick duplicado), e uma perda silenciosa de dados de auditoria (log deletado sem ledger). Nenhum dos 29 achados foi coberto pelos audits anteriores.

---

## Dedup — Varredura cruzada com audits anteriores

Esta auditoria deduplicou contra dois anchors:

### Anchor 1: docs/audit/2026-06-18-768-bug-audit.md (branch auto/issue-768)

O audit #768 (25 bugs) cobre `deile/core/`, `deile/config/`, `deile/cron/`, `deile/events/`, `deile/tools/`, `deile/memory/`, `deile/security/`, `deile/storage/usage_repository.py`, `deile/parsers/`, `deile/infrastructure/observability/`, `deile/integrations/`. A sobreposição de arquivos com o presente audit é mínima: `session_store.py` (lock de init do SessionStore — já corrigido em HEAD com `self._lock = asyncio.Lock()` na linha 54), `bot/client.py` (singleton sem threading.Lock — distinto de `agent.py` e `debug_logger.py`). Nenhum dos 29 bugs aqui listados coincide com entradas do #768; onde há padrão similar (singleton sem lock, sync I/O), o arquivo é distinto e foi verificado individualmente.

### Anchor 2: docs/audit/2026-05-29-pipeline-audit-findings.md

O audit de 2026-05-29 (13 achados) cobre `deile/orchestration/pipeline/`, `deile/orchestration/forge/github_forge.py`, `infra/k8s/{claude_worker_server,worker_server,deploy}.py`. As sobreposições verificadas foram: (a) sync I/O em `claude_worker_server.py:682-697` — arquivo distinto de `cli_worker_server.py:1115-1125`; (b) `usage_repository.py: check_all sem lock` — race na leitura, distinto do bug de sync I/O via `sqlite3.connect()` em `cost_estimator.py`; (c) `github_forge.py:740,751` — jq injection, distinto de `gitlab_forge.py:branch_exists` e `label_applied_at`. Nenhuma entrada coincide.

---

## Achados

### 1. 🔴 Router health-check sem timeout — trava select_provider() indefinidamente

**Categoria**: timeout
**Arquivo**: `deile/core/models/router.py:239-259`

`_health_check_if_needed()` itera todos os providers registrados sequencialmente, aguardando `await provider.health_check()` sem nenhum `asyncio.wait_for()`. Cada `health_check()` chama `self.generate()` com uma chamada de rede real sem timeout (porque `timeout_seconds` nunca é repassado ao SDK — ver achado #2). Se qualquer provider travar indefinidamente em TCP keepalive, `select_provider()` bloqueia para sempre, já que `_health_check_if_needed()` é a primeira chamada em cada seleção de provider.

```python
# router.py:109
await self._health_check_if_needed()

# router.py:248-250
for provider in providers:
    is_healthy = await provider.health_check()  # sem asyncio.wait_for()
```

**Cenário de reprodução**: Durante um ciclo de health-check (a cada 5 minutos, `health_check_interval=300`), qualquer endpoint de provider inacessível (TCP SYN enviado, sem RST/ACK) bloqueia `select_provider()` por todo o timeout de TCP do kernel, paralisando todas as turns de agente que disparam o health-check.

**Comportamento esperado**: Cada `health_check()` deve ser encapsulado com `asyncio.wait_for(provider.health_check(), timeout=30.0)`, de modo que um provider travado não bloqueie todo o caminho de roteamento.

**Fix sugerido**: Em `_health_check_if_needed()` em `router.py:250`, encapsular com: `is_healthy = await asyncio.wait_for(provider.health_check(), timeout=30.0)`. Alternativamente, rodar todos os health-checks concorrentemente com `asyncio.gather()` e timeouts por tarefa.

---

### 2. 🟢 `timeout_seconds` declarado mas nunca consumido por nenhum provider

**Categoria**: doc-code-divergence
**Arquivo**: `deile/core/models/provider_config.py:18`

`ProviderConfig.timeout_seconds` é declarado com valor 120 e configurado em `model_providers.yaml` para todos os providers (linhas 19, 28, 35, 42, 55), mas nunca consumido por nenhum provider concreto. `AnthropicProvider`, `OpenAIProvider` e `GeminiProvider` ignoram o campo ao construir seus clientes SDK.

```python
# provider_config.py:18
timeout_seconds: int = 120

# anthropic_provider.py:65-72 — sem timeout= no construtor
sdk_kwargs = dict(provider_config.sdk_kwargs or {})
self._client = AsyncAnthropic(**sdk_kwargs)
```

**Cenário de reprodução**: Operador define `timeout_seconds: 30` no YAML para prevenir chamadas lentas. A configuração não tem efeito — todos os providers usam o timeout padrão do SDK.

**Comportamento esperado**: Cada provider deve repassar `provider_config.timeout_seconds` ao construtor do cliente SDK.

**Fix sugerido**: Em `AnthropicProvider.__init__`, passar `timeout=provider_config.timeout_seconds` para `AsyncAnthropic`. Em `OpenAIProvider.__init__`, para `AsyncOpenAI`. Em `GeminiProvider`, via `HttpOptions`.

---

### 3. 🔴 GeminiProvider chama `_llm_span_cm.__exit__(None, None, None)` mesmo com exceção ativa — span marcado como SUCCESS

**Categoria**: doc-code-divergence
**Arquivo**: `deile/core/models/gemini_provider.py:520-524`

O bloco `finally` em `GeminiProvider.generate()` chama `_llm_span_cm.__exit__(None, None, None)` mesmo quando uma exceção está propagando, informando ao context manager OTLP que não ocorreu exceção. Os outros providers usam `with self._llm_span() as _span:` que corretamente propaga `exc_info` para `__exit__`. O comentário em `tracer.py:231` afirma que o SDK marca o span via `record_exception` no `__exit__` — esse mecanismo é bypassado aqui.

```python
# gemini_provider.py:405-406
_llm_span_cm = self._llm_span()
_llm_span = _llm_span_cm.__enter__()

# gemini_provider.py:520-524
finally:
    try:
        _llm_span_cm.__exit__(None, None, None)  # ERRADO: sempre None, mesmo com exceção
    except Exception:
        pass
```

**Cenário de reprodução**: Qualquer request através de `GeminiProvider.generate()` que lança `ProviderInvocationError`. O span OTLP é marcado SUCCESS em vez de ERROR.

**Comportamento esperado**: Usar `with self._llm_span() as _llm_span:` como nos providers Anthropic e OpenAI, permitindo que o CM veja a exceção real.

**Fix sugerido**: Substituir o padrão manual `__enter__`/`__exit__` por `with self._llm_span() as _llm_span:` envolvendo todo o bloco try/except.

---

### 4. 🟡 Sync I/O bloqueante em `_build_preferences_block` — `store.get_all()` sem `asyncio.to_thread`

**Categoria**: sync-io-in-async
**Arquivo**: `deile/core/context_manager.py:26-46`

`_build_preferences_block` é `async def` mas chama `store.get_all(user_id)` de forma síncrona. `PreferenceStore.get_all` chama `_read_prefs_file()` que executa `open(_PREFS_FILE)` + `json.load()` bloqueantes. Contrasta com `_prepend_deile_md_layers` (linha 98) que corretamente usa `await asyncio.to_thread(loader.build_merged_prompt)`.

```python
# context_manager.py:38
prefs = store.get_all(user_id)  # ERRADO: sem await, sem asyncio.to_thread
```

**Cenário de reprodução**: Sessão de bot com preferências de usuário — `build_context → _build_system_instruction → _build_preferences_block → PreferenceStore.get_all → open() + json.load()` tudo síncrono dentro do event loop.

**Comportamento esperado**: `prefs = await asyncio.to_thread(store.get_all, user_id)`.

**Fix sugerido**: Alterar linha 38 para `prefs = await asyncio.to_thread(store.get_all, user_id)`.

---

### 5. 🟡 Sync I/O bloqueante em `_build_file_context` — `os.walk()` sem `asyncio.to_thread`

**Categoria**: sync-io-in-async
**Arquivo**: `deile/core/context_manager.py:516-565`

`_build_file_context` é `async def` mas executa `os.walk(work_dir)` de forma síncrona. Em projetos grandes (milhares de arquivos), isso bloqueia o event loop por centenas de milissegundos a cada turn.

```python
# context_manager.py:560
for root, dirs, files in os.walk(work_dir):  # bloqueante, sem asyncio.to_thread
```

**Cenário de reprodução**: A cada turn, `build_context → _build_system_instruction → _build_file_context` executa `os.walk` sincronamente no diretório do projeto.

**Comportamento esperado**: Extrair a lógica de walk em função helper síncrona e chamar via `await asyncio.to_thread(_scan_files, work_dir, ...)`.

**Fix sugerido**: Extrair o loop em `_scan_files(work_dir, ignore_dirs, ignore_ext)` síncrono e chamar como `file_list = await asyncio.to_thread(_scan_files, ...)`.

---

### 6. 🔴 `process_input_stream_chunks` checa `"TOOL_INVOKED"` — evento que não existe em `StreamEventType`

**Categoria**: correctness
**Arquivo**: `deile/core/agent_streaming.py:745-751`

O branch `elif name in ("TOOL_INVOKED", "tool_invoked"):` para emitir `tool_call_started` StreamChunks é dead code: `StreamEventType` não tem esse valor. Os nomes reais são `TOOL_USE_START`, `TOOL_USE_END`, `TOOL_RESULT`. Consumidores bot nunca recebem `tool_call_started`.

```python
# agent_streaming.py:745
elif name in ("TOOL_INVOKED", "tool_invoked"):  # ERRADO: nunca verdadeiro
    yield StreamChunk("tool_call_started", ...)
```

**Cenário de reprodução**: Bot consumer chama `process_input_stream_chunks` esperando eventos `tool_call_started`. O agente chama uma ferramenta. Nenhum chunk é emitido.

**Comportamento esperado**: Verificar `name in ("TOOL_USE_START", "tool_use_start")`.

**Fix sugerido**: Alterar linha 745 para `elif name in ("TOOL_USE_START", "tool_use_start"):`.

---

### 7. 🔴 ERROR chunk no bot streaming usa atributos inexistentes — `error_type` e `error_message` sempre vazios

**Categoria**: correctness
**Arquivo**: `deile/core/agent_streaming.py:765-769`

O tratamento de eventos ERROR lê `getattr(evt, "error_type", "")` e `getattr(evt, "error_message", "")` diretamente do objeto `UnifiedStreamEvent`. Esses atributos não existem — o dado real está em `evt.error_envelope` (um dict). Consumidores bot sempre recebem type e message vazios.

```python
# agent_streaming.py:767-768
"type": getattr(evt, "error_type", ""),    # sempre "" — campo não existe
"message": getattr(evt, "error_message", ""),  # sempre "" — campo não existe

# Correto seria:
envelope = getattr(evt, "error_envelope", None) or {}
"type": envelope.get("error_type", "") or "Error"
"message": envelope.get("message", "")
```

**Cenário de reprodução**: Bot recebe BudgetExceeded, ModelError, etc. O chunk ERROR é emitido com `{"type": "", "message": ""}` — toda informação diagnóstica perdida.

**Comportamento esperado**: `(evt.error_envelope or {}).get("error_type", "")` e `(evt.error_envelope or {}).get("message", "")`.

**Fix sugerido**: Substituir linhas 767-768 por acesso via `evt.error_envelope`.

---

### 8. 🔴 Validation gate vaza entradas fantasma no histórico quando retry falha

**Categoria**: state-leak
**Arquivo**: `deile/core/validation_gate.py:175-176, 197`

Quando o validation gate dispara, ele adiciona duas entradas ao histórico (`assistant` + `user`) antes de invocar retry. Se o retry lança `Exception` (capturado na linha 189), o gate retorna o resultado pré-gate — mas as duas entradas fantasma permanecem em `session.conversation_history`. Turns subsequentes enviam essas mensagens `[INTERNAL_VALIDATION_GATE]` ao LLM, corrompendo o contexto.

```python
# validation_gate.py:175-176
session.add_to_history("assistant", content, {"validation_gate_pre": True})
session.add_to_history("user", gate_prompt, {"validation_gate": True})

# validation_gate.py:189-197 — sem rollback do histórico no except
except Exception:
    ...
    return content, tool_results  # entradas fantasma permanecem!
```

**Cenário de reprodução**: Turn dispara o gate de escrita não-validada. A chamada ao provider retry falha (erro de rede, budget). O próximo turn do usuário vê as entradas `[INTERNAL_VALIDATION_GATE]` no contexto, confundindo o modelo.

**Comportamento esperado**: Salvar `len(session.conversation_history)` antes das injeções e restaurar no branch `except`.

**Fix sugerido**: Antes da linha 175, adicionar `_history_checkpoint = len(session.conversation_history)`. No `except Exception` (linha 193), antes do `return`: `del session.conversation_history[_history_checkpoint:]`.

---

### 9. 🟡 TOCTOU em `get_session_store()` — duas corrotinas criam SessionStores duplicados

**Categoria**: TOCTOU
**Arquivo**: `deile/core/agent.py:880-898`

`get_session_store()` tem um padrão lazy-init sem lock: `if not hasattr(self, "_session_store") or self._session_store is None:` seguido de `await store.init()`. Duas corrotinas concorrentes podem ambas passar na verificação, ambas chamar `SessionStore(path)` e `await store.init()`, criando dois pools de conexão SQLite separados para o mesmo arquivo.

```python
# agent.py:882-898
async def get_session_store(self):
    if not hasattr(self, "_session_store") or self._session_store is None:
        store = SessionStore(path)
        await store.init()          # yield point — outra corrotina pode entrar aqui
        self._session_store = store  # segunda atribuição silenciosamente descarta a primeira
```

**Cenário de reprodução**: Dois requests bot concorrentes para o mesmo agente chamam `get_or_create_session(session_id, persisted=True)` simultaneamente.

**Comportamento esperado**: `async with self._store_init_lock: if self._session_store is None: ...`.

**Fix sugerido**: Adicionar `self._session_store_lock = asyncio.Lock()` em `__init__` e envolver o body em `async with self._session_store_lock:` com double-check após aquisição.

---

### 10. 🟢 `get_agent()` singleton sem threading.Lock — duas threads criam dois DeileAgent

**Categoria**: state-leak
**Arquivo**: `deile/core/agent.py:2086-2093`

`_agent` singleton de nível de módulo e `get_agent()` são estado mutável desprotegido. Duas threads chamando `get_agent()` podem ambas passar pelo check `if _agent is None`, resultando em duas instâncias `DeileAgent` separadas onde os callers esperam um singleton.

```python
# agent.py:2089-2093
def get_agent() -> DeileAgent:
    global _agent
    if _agent is None:                # sem threading.Lock
        _agent = DeileAgent()
    return _agent
```

**Cenário de reprodução**: Dois threads chamam `get_agent()` antes de qualquer um terminar a inicialização. Skills watcher e session store da instância descartada vazam.

**Comportamento esperado**: Usar `threading.Lock` para guardar a criação do singleton.

**Fix sugerido**: Adicionar `_agent_lock = threading.Lock()` no nível de módulo. Em `get_agent()`: `with _agent_lock: if _agent is None: _agent = DeileAgent()`.

---

### 11. 🟢 `_PLAIN_CONSOLE` singleton em `agent_streaming.py` sem lock — captura Rich não é thread-safe

**Categoria**: state-leak
**Arquivo**: `deile/core/agent_streaming.py:91`

`_PLAIN_CONSOLE` é um singleton mutável compartilhado inicializado lazily sem lock. Dois threads chamando `_normalize_history_content` simultaneamente podem ambos criar um `Console` — um é descartado. Mais criticamente, dois threads em `_PLAIN_CONSOLE.capture()` concorrentemente podem corromper o output de uma captura com o da outra.

**Cenário de reprodução**: O adaptador bot despacha duas turns concorrentes que produzem Rich renderables em resultados de tool. Ambas chamam `_normalize_history_content(panel)` em threads separadas.

**Comportamento esperado**: Lock por volta da inicialização E do bloco de captura, ou criar `Console` por chamada.

**Fix sugerido**: Adicionar `_PLAIN_CONSOLE_LOCK = threading.Lock()` e envolver as linhas 69-83 com `with _PLAIN_CONSOLE_LOCK:`.

---

### 12. 🟡 Slash commands adicionados ao histórico antes da detecção no caminho de streaming

**Categoria**: state-leak
**Arquivo**: `deile/core/agent_streaming.py:91`

No caminho de streaming, `session.add_to_history("user", user_input)` é chamado na linha 91 incondicionalmente antes da verificação de slash command na linha 96. O `return` antecipado na linha 174 sai após yield da resposta do slash command sem remover essa entrada do histórico. O caminho não-streaming em `agent.py:577-589` tem a correção correta com guarda `if not _is_known_slash`.

```python
# agent_streaming.py:91
session.add_to_history("user", user_input)  # ERRADO: antes de checar slash

# agent.py:582-587 — padrão correto, não aplicado ao streaming:
if not _is_known_slash:
    session.add_to_history("user", user_input)
```

**Cenário de reprodução**: Usuário digita `/clear`, `/rewind`, `/help`. O slash é processado e respondido, mas a entrada fica no histórico — LLM vê `/clear` como mensagem de usuário nas turns seguintes.

**Comportamento esperado**: Mover `add_to_history` para após a detecção do slash command, espelhando `agent.py:577-589`.

**Fix sugerido**: Reordenar linhas em `agent_streaming.py` para aplicar a guarda `if not _is_known_slash` antes de `add_to_history`.

---

### 13. 🟡 `_request_count` e `_status` mutados sem sincronização em `process_input`

**Categoria**: concurrency
**Arquivo**: `deile/core/agent.py:534-536`

`process_input` muta `self._status` e `self._request_count` sem sincronização. Com múltiplos requests concorrentes, `_status` pode oscilar entre corrotinas: Request A define PROCESSING→IDLE enquanto Request B ainda está rodando, reportando IDLE incorretamente.

```python
# agent.py:535-536 (e identicamente em agent_streaming.py:63-64)
self._status = AgentStatus.PROCESSING   # sem lock
self._request_count += 1               # sem lock
```

**Cenário de reprodução**: Duas sessões bot chamam `process_input` concorrentemente. Request A termina e define IDLE enquanto B ainda processa — servidor de status reporta IDLE incorretamente.

**Comportamento esperado**: Documentar a limitação explicitamente, ou usar status por-sessão em vez de por-agente.

**Fix sugerido**: Para uso exclusivamente de observabilidade, documentar a limitação. Para corrigir: envolver com `asyncio.Lock`.

---

### 14. 🔴 `ForgeClient._run()` sem timeout em `proc.communicate()` — forge CLI pode travar indefinidamente

**Categoria**: timeout
**Arquivo**: `deile/orchestration/forge/base.py:350-355`

`ForgeClient._run()` chama `asyncio.create_subprocess_exec` + `proc.communicate()` sem `asyncio.wait_for()`. Toda invocação forge CLI (gh api, glab api, etc.) pode travar indefinidamente se o subprocesso parar.

```python
# base.py:350-355
proc = await asyncio.create_subprocess_exec(*cmd, stdout=PIPE, stderr=PIPE)
stdout_b, stderr_b = await proc.communicate()  # sem asyncio.wait_for()
```

**Cenário de reprodução**: GitLab lento ou chamada `glab api --paginate` em um resource-label-events grande faz o subprocesso rodar por minutos. O event loop trava permanentemente.

**Comportamento esperado**: `await asyncio.wait_for(proc.communicate(), timeout=self._config.cli_timeout_s)`.

**Fix sugerido**: Envolver `await proc.communicate()` em `asyncio.wait_for(..., timeout=N)` e adicionar `proc.kill()` handler no `TimeoutError`.

---

### 15. 🟢 `asyncio.get_event_loop()` deprecated em `detection.py:302` — deve ser `get_running_loop()`

**Categoria**: other
**Arquivo**: `deile/orchestration/forge/detection.py:302`

Dentro de `async def _do_probe()`, `asyncio.get_event_loop()` é usado em vez de `asyncio.get_running_loop()`. Deprecated desde Python 3.10; em Python 3.12+ pode lançar `RuntimeError` em algumas configurações de thread.

```python
# detection.py:302
async def _do_probe(host: str):
    loop = asyncio.get_event_loop()   # deprecated; deve ser get_running_loop()
    result = await loop.run_in_executor(...)
```

**Cenário de reprodução**: Python 3.12+ em configuração onde nenhum event loop está definido no thread — `get_event_loop()` lança `RuntimeError` em vez de retornar o running loop.

**Comportamento esperado**: `loop = asyncio.get_running_loop()`.

**Fix sugerido**: Substituir linha 302 por `loop = asyncio.get_running_loop()`.

---

### 16. 🟡 `branch_exists()` em `gitlab_forge.py` — dois calls sequenciais à mesma API criam janela TOCTOU

**Categoria**: TOCTOU
**Arquivo**: `deile/orchestration/forge/gitlab_forge.py:1367-1379`

`branch_exists()` faz dois calls sequenciais ao mesmo endpoint: o primeiro com `--silent` descarta o body; se rc != 0, um segundo call captura o body para checar '404'. Entre os dois calls, o estado da branch pode mudar, produzindo resultado contraditório.

```python
# gitlab_forge.py:1367-1379
rc, _, _ = await self._run('api', ..., '--silent')   # body descartado
if rc != 0:
    rc2, body, _ = await self._run('api', ...)       # segundo call ao mesmo endpoint
    return "404" not in (body or "")[:200].lower()
```

**Cenário de reprodução**: Erro transitório 503 no call 1 (rc != 0), depois 404 real no call 2 — função retorna False (branch ausente) quando a branch existia no momento do call 1.

**Comportamento esperado**: Um único call API que captura rc e body juntos.

**Fix sugerido**: Remover o primeiro call silencioso e fazer um único `rc, body, err = await self._run('api', ...)` parsando o body diretamente.

---

### 17. 🟡 `label_applied_at()` — comparação lexicográfica de timestamps ISO pode selecionar evento errado

**Categoria**: correctness
**Arquivo**: `deile/orchestration/forge/gitlab_forge.py:1065`

Em `label_applied_at()`, o evento 'mais recente' é determinado por comparação lexicográfica de strings `created > latest_iso`. Se a API retornar sufixos de timezone mistos (Z vs +00:00), a ordenação lexicográfica é incorreta: `2023-01-15T10:30:00.999Z` < `2023-01-15T10:30:01+00:00` lexicograficamente ('+' < '.'), mas temporalmente é maior.

```python
# gitlab_forge.py:1065
if latest_iso is None or created > latest_iso:  # comparação de string, não datetime
```

**Cenário de reprodução**: API GitLab retorna timestamps com sufixos mistos (Z vs +00:00) dentro da mesma resposta — reaper calcula tempo de aplicação de label errado e pode tratar claim fresh como stale.

**Comportamento esperado**: Parsear `created_at` para objetos datetime antes de comparar, consistente com a conversão final em linha 1071-1072.

**Fix sugerido**: Substituir a comparação na linha 1065 por `datetime.fromisoformat(created.replace('Z', '+00:00')) > datetime.fromisoformat(latest_iso.replace('Z', '+00:00'))`.

---

### 18. 🟡 `get_work_item_details()` classifica `"refs"` e `"references"` como `"closes"` — classificador incorreto

**Categoria**: doc-code-divergence
**Arquivo**: `deile/orchestration/forge/gitlab_forge.py:932-938`

O helper `_links()` classifica itens linkados usando o primeiro caractere do match: `'c'→closes`, `'f'→closes`, `'r'→closes`. Mas `'r'` cobre tanto `resolv*` (que deve ser 'closes') quanto `ref*` / `refs` / `references` (que deve ser 'refs'), tornando impossível a emissão do kind `'refs'` conforme documentado em `base.py:185`.

```python
# gitlab_forge.py:934
"closes" if m.group(0).lower()[0] in "cfr" else "refs"
# 'refs' começa com 'r' → sempre classificado como "closes"
```

**Cenário de reprodução**: MR body contém `"References #15"`. `_links()` retorna `[('closes', 15)]` em vez de `[('refs', 15)]`. Pipeline pode tratar referência como intenção de fechar issue.

**Comportamento esperado**: Distinguir keywords `ref*` (→'refs') de `resolv*` (→'closes').

**Fix sugerido**: Substituir condição por `'closes' if m.group(0).lower().startswith(("clos", "fix", "resolv")) else 'refs'`.

---

### 19. 🟡 Subprocess orphan leak em `_collect_review_delta` — `gh` não é morto no timeout

**Categoria**: error-handling
**Arquivo**: `deile/orchestration/pipeline/implementer.py:1253-1269 e 1273-1290`

Dois blocos de subprocesso usam `asyncio.wait_for(proc.communicate(), timeout=10)` mas nunca chamam `proc.kill()` quando o timeout dispara. O `except Exception: pass` captura `asyncio.TimeoutError` silenciosamente, deixando o processo `gh` rodando como orphan.

```python
# implementer.py:1262
out, _ = await _aio.wait_for(proc.communicate(), timeout=10)
# ...
except Exception: pass  # TimeoutError engolido, proc nunca morto
```

**Cenário de reprodução**: GitHub API lento — timeout dispara, `gh` continua rodando, consumindo FD e tokens da API. Calls repetidos acumulam orphans.

**Comportamento esperado**: No `TimeoutError`, chamar `proc.kill()` + `await proc.wait()` como em `claude_dispatcher.py:115-117`.

**Fix sugerido**: Adicionar `except asyncio.TimeoutError: proc.kill(); await proc.wait()` em ambos os blocos.

---

### 20. 🔴 `cost_estimator.py` — sync I/O bloqueante (`open()` + `sqlite3.connect()`) em `async def _dispatch()`

**Categoria**: sync-io-in-async
**Arquivo**: `deile/orchestration/pipeline/cost_estimator.py:72`

`_load()` usa `open(yaml_file)` síncrono (linha 72) e `usage_repository.py` usa `sqlite3.connect()` síncrono. Ambos são chamados a partir de `async def _dispatch()` sem `asyncio.to_thread()`, bloqueando o event loop em cada dispatch.

```python
# cost_estimator.py:72
with open(yaml_file) as fh:   # bloqueante, dentro de cadeia async
    ...

# usage_repository.py:77-78
def _connect(self):
    return sqlite3.connect(...)  # bloqueante, chamado de async _dispatch()
```

**Cenário de reprodução**: Cada dispatch chama `_guard.check_stage_run()` → `cost_estimator.estimate_run_cost()` → `open()` + `sqlite3.connect()` bloqueantes no event loop.

**Comportamento esperado**: Encapsular em `asyncio.to_thread()` ou tornar os métodos async.

**Fix sugerido**: Encapsular `check_stage_run()` no call site com `await asyncio.to_thread(...)`, ou tornar `estimate_run_cost()` e `records_for_stage_model()` async.

---

### 21. 🟢 Docstring de `_collect_review_delta` cita `subprocess.run` — código usa `asyncio.create_subprocess_exec`

**Categoria**: doc-code-divergence
**Arquivo**: `deile/orchestration/pipeline/implementer.py:1242`

Docstring diz "via subprocess.run" mas o código usa `asyncio.create_subprocess_exec`. Developer pode escrever mocks errados ou fazer suposições incorretas sobre bloqueio.

```python
# implementer.py:1242
"""Roda LOCAL no pipeline pod via subprocess.run — `gh` está no PATH"""  # ERRADO

# implementer.py:1253, 1273
proc = await _aio.create_subprocess_exec(...)  # correto
```

**Fix sugerido**: Substituir `subprocess.run` na docstring por `asyncio.create_subprocess_exec`.

---

### 22. 🔴 Git subprocessos em `WorktreeManager` sem timeout — clone/fetch/pull pode travar forever

**Categoria**: timeout
**Arquivo**: `deile/orchestration/pipeline/worktree_manager.py:322-332 e 343-354`

Todos os helpers git (`_git`, `_git_in`, `_git_in_capture`) chamam `proc.communicate()` sem `asyncio.wait_for()`. Operações git de rede (`clone`, `fetch`, `pull`) em `ensure_main()` podem travar indefinidamente.

```python
# worktree_manager.py:328
stdout_b, stderr_b = await proc.communicate()  # sem timeout

# worktree_manager.py:349
stdout_b, stderr_b = await proc.communicate()  # sem timeout
```

**Cenário de reprodução**: GitHub inacessível durante `ensure_main()` → `_git('clone', ...)`. O event loop trava; monitor não processa outros issues, não envia heartbeats.

**Comportamento esperado**: `await asyncio.wait_for(proc.communicate(), timeout=GIT_OP_TIMEOUT_S)` com `proc.kill()` no `TimeoutError`.

**Fix sugerido**: Envolver `proc.communicate()` nos helpers `_git` e `_git_in_capture` com `asyncio.wait_for(..., timeout=60)`.

---

### 23. 🔴 Log deletado sem ledger quando `has_tokens=False` em `cli_worker_server.py`

**Categoria**: correctness
**Arquivo**: `infra/k8s/cli_worker_server.py:1115-1125`

Logs são deletados incondicionalmente mesmo quando `has_tokens` é False, violando o contrato da docstring "Remove SOMENTE após contabilizar." Quando o CLI crashou antes de emitir usage metadata, o log é apagado permanentemente sem registro no ledger.

```python
# cli_worker_server.py:1092
has_tokens = any(...)  # pode ser False se CLI crashou

# cli_worker_server.py:1093
if task_id not in harvested and has_tokens:
    # escreve ledger — SKIPPED quando has_tokens=False

# cli_worker_server.py:1115-1120 — executado INCONDICIONALMENTE:
for sibling in log_file.parent.glob(f"{task_id}.*"):
    sibling.unlink()  # apaga log sem ledger!
```

**Cenário de reprodução**: CLI task completa mas não emite token usage (crash mid-run). `parse_progress_text` sucede (não None) mas retorna modelos vazios. `has_tokens=False`. Log apagado, ledger não escrito — audit trail perdido permanentemente.

**Comportamento esperado**: Deleção do log SOMENTE após escrita bem-sucedida no ledger, ou quando `task_id in harvested`.

**Fix sugerido**: Mover o bloco de deleção para dentro do `if task_id not in harvested and has_tokens:` após escrita bem-sucedida no ledger, adicionando `continue` no else.

---

### 24. 🟢 `debug_logger.py` — singleton sem lock pode resetar `request_count` a 0 em race de thread

**Categoria**: state-leak
**Arquivo**: `deile/storage/debug_logger.py:93-100`

`get_debug_logger()` é criado sem lock. Dois threads chamando simultaneamente podem ambos criar `_DebugLogger()` — o segundo sobrescreve o primeiro, zerando `request_count`.

```python
# debug_logger.py:97-100
if _singleton is None:              # sem threading.Lock
    _singleton = _DebugLogger()
return _singleton
```

**Fix sugerido**: Inicializar `_singleton = _DebugLogger()` no nível de módulo diretamente, eliminando lazy init.

---

### 25. 🟢 `EmbeddingStore.__iter__` anotado como `Iterable` — deve ser `Iterator`

**Categoria**: doc-code-divergence
**Arquivo**: `deile/storage/embeddings.py:21`

`__iter__` deve retornar `Iterator` (tem `__next__`), não `Iterable` (que tem apenas `__iter__`). O valor retornado `iter(self._items)` é correto em runtime; apenas a anotação está errada.

```python
# embeddings.py:21
def __iter__(self) -> Iterable[dict[str, Any]]:  # ERRADO: deve ser Iterator
    return iter(self._items)
```

**Fix sugerido**: Alterar para `def __iter__(self) -> Iterator[dict[str, Any]]:` e importar `Iterator` de `typing`.

---

### 26. 🟡 `_load_mention_cursor` e `_save_mention_cursor` em `monitor.py` — sync I/O no event loop

**Categoria**: doc-code-divergence
**Arquivo**: `deile/orchestration/pipeline/monitor.py:1016-1031`

`_load_mention_cursor` (linha 1021: `read_text()`) e `_save_mention_cursor` (linha 1030: `write_text()`) são métodos síncronos chamados de dentro de `async def process_mentions` (stages.py:484, 497, 532), bloqueando o event loop em cada tick com `enable_mention_handling=True`.

```python
# monitor.py:1021
def _load_mention_cursor(self) -> ...:
    ... = self._mention_cursor_path.read_text()  # bloqueante, chamado de async

# stages.py:484
monitor._save_mention_cursor(...)  # sem await
```

**Comportamento esperado**: Tornar os métodos `async def` e usar `await asyncio.to_thread(...)` para as operações de I/O.

**Fix sugerido**: Converter para `async def` e atualizar os 3 call sites em `stages.py` com `await`.

---

### 27. 🔴 `force-tick` via `asyncio.ensure_future` cria tick concorrente — double-dispatch possível

**Categoria**: concurrency
**Arquivo**: `deile/orchestration/pipeline/runner.py:123-124`

`_force_tick_cb` usa `asyncio.ensure_future(monitor.tick())` enquanto `_run_forever` pode já estar aguardando `self.tick()`. `PipelineMonitor.tick()` não tem guard contra invocação concorrente — dois ticks paralelos podem ambos selecionar o mesmo issue `~workflow:revisada` e fazer double-dispatch.

```python
# runner.py:123-124
def _force_tick_cb():
    asyncio.ensure_future(monitor.tick())  # cria tick paralelo sem guard
```

**Cenário de reprodução**: Operador chama `POST /v1/pipeline/force-tick`. Monitor está no meio do implement stage. Dois ticks executam `implement_one_reviewed_issue` concorrentemente — mesmo issue `~workflow:revisada` é despachado duas vezes antes de qualquer um commitar transição de label.

**Comportamento esperado**: force-tick deve verificar se um tick já está em andamento e pular ou enfileirar.

**Fix sugerido**: Adicionar flag `_tick_in_flight: bool = False`. Em `tick()`: `self._tick_in_flight = True` no início e `False` no `finally`. Em `_force_tick_cb`: `if not monitor._tick_in_flight: asyncio.ensure_future(monitor.tick())`.

---

### 28. 🟡 `start_workflow_execution` sempre retorna `total_steps: 0` — stale in-memory reference

**Categoria**: correctness
**Arquivo**: `deile/orchestration/workflow_executor.py:119-127`

`start_workflow_execution` sempre retorna `total_steps: 0` e `execution_info.total_tasks: 0`. A task list é criada com `total_tasks=0` (padrão do dataclass). Calls a `add_task_to_list` atualizam o contador no DB via SQL subquery mas nunca atualizam o atributo in-memory `task_list.total_tasks`.

```python
# workflow_executor.py:54-65
task_list = await create_task_list(...)  # total_tasks=0
for step in steps:
    await add_task_to_list(...)  # atualiza DB, não o objeto in-memory

# workflow_executor.py:122, 126
"total_steps": task_list.total_tasks  # stale 0
```

**Cenário de reprodução**: Caller usa `total_steps` para progress display ou asserções — sempre vê 0.

**Fix sugerido**: Após o loop, `task_list = await self.task_manager.load_task_list(task_list.id) or task_list`, ou usar `len(steps)` diretamente.

---

### 29. 🟢 `asyncio.get_event_loop()` deprecated em 4 call sites de `sqlite_task_manager.py`

**Categoria**: other
**Arquivo**: `deile/orchestration/sqlite_task_manager.py:319, 434, 462, 588`

Quatro call sites usam `asyncio.get_event_loop().time()` — deprecated desde Python 3.10. A linha 588 está em método síncrono `_is_cache_valid` que seria incorreto se chamado sem event loop ativo.

```python
# sqlite_task_manager.py:319
self._cache_timestamps[list_id] = asyncio.get_event_loop().time()  # deprecated
```

**Fix sugerido**: Substituir por `asyncio.get_running_loop().time()` nas linhas async (319/434/462) e `time.monotonic()` na linha síncrona (588).

---

### 30. 🔴 `_execute_task_list_loop` swallows Exception sem marcar tasks como FAILED — `wait_for_workflow_completion` trava por 1h

**Categoria**: error-handling
**Arquivo**: `deile/orchestration/workflow_executor.py:156-157`

`_execute_task_list_loop` captura `Exception` silenciosamente, terminando o loop mas deixando tasks em estado TODO. `wait_for_workflow_completion` então faz poll indefinidamente porque `completed_tasks != total_tasks` e `failed_tasks == 0` — nunca atinge `is_completed=True` até o timeout de 1 hora.

```python
# workflow_executor.py:156-157
except Exception as exc:
    logger.error("Workflow loop %s aborted: %s", list_id, exc)
    # tasks ficam em TODO — wait_for_workflow_completion trava por 1h
```

**Cenário de reprodução**: Erro de aiosqlite ou qualquer exceção de infra aborta o loop. Caller em `wait_for_workflow_completion` fica preso até 1h.

**Comportamento esperado**: No `except`, marcar a task in-progress como FAILED para que `wait_for_workflow_completion` possa sair com `has_failures=True`.

**Fix sugerido**: No handler de exceção: `await self.task_manager.mark_task_completed(list_id=list_id, task_id=current_task.id, success=False, error_message=str(exc))` antes de retornar.

---

## Top-10 priorizado

| # | Bug | Arquivo | Severidade | Gatilho | Blast | Justif. Blast | Fix proposto | Arquivos impactados | Testes impactados | Docs impactados |
|---|-----|---------|-----------|---------|-------|----------------|--------------|--------------------|--------------------|-----------------|
| 1 | force-tick double-dispatch via ensure_future | `runner.py:123-124` | 🔴 crítico | Dois ticks paralelos double-dispatching mesmo issue — resultado errado silencioso, corrupção de estado do pipeline | L | Fix toca caminho concorrente/distribuído com risco de deadlock se lock mal-escoped; alternativa (wake flag) toca múltiplos arquivos | Adicionar `_tick_in_flight: bool` em `PipelineMonitor.__init__`; guardar em `tick()` com finally; checar em `_force_tick_cb` | `runner.py`, `monitor.py` | `test_runner.py`, `test_monitor_tick.py` | `docs/decisoes/pipeline.md` |
| 2 | Validation gate vaza histórico fantasma no except | `validation_gate.py:175-176,197` | 🔴 crítico | Retry falha → duas entradas `[INTERNAL_VALIDATION_GATE]` ficam em `conversation_history` → LLM recebe contexto corrompido em toda turn subsequente | S | 1 arquivo, ~5 linhas: checkpoint + del slice no except | `_history_checkpoint = len(session.conversation_history)` antes de linha 175; no `except`: `del session.conversation_history[_history_checkpoint:]` | `validation_gate.py` | `test_validation_gate.py` | — |
| 3 | Log deletado sem ledger quando `has_tokens=False` | `cli_worker_server.py:1115-1125` | 🔴 crítico | CLI crashou sem emitir usage → log apagado sem ledger — perda silenciosa de audit trail | S | 1 arquivo, ≤5 linhas: mover deleção para dentro do bloco `has_tokens` | Mover bloco de deleção (linhas 1115-1120) para dentro do `if has_tokens and ...` após ledger write; `continue` no else | `cli_worker_server.py` | `test_harvest_log.py` | docstring linha 1013 |
| 4 | `_execute_task_list_loop` trava `wait_for_workflow_completion` por 1h | `workflow_executor.py:156-157` | 🔴 crítico | Erro de infra aborta loop → tasks em TODO → poll sem saída por 1h | S | 1 arquivo, ~5 linhas no except handler | No `except`: `await self.task_manager.mark_task_completed(..., success=False, error_message=str(exc))` | `workflow_executor.py` | `test_workflow_executor.py` | — |
| 5 | Router health-check sem timeout — trava select_provider() | `router.py:239-259` | 🔴 crítico | Provider inacessível → health_check() trava forever → toda seleção de LLM bloqueada | M | Toca `router.py` e `base.py`, novo teste de timeout | `asyncio.wait_for(provider.health_check(), timeout=30.0)` em `_health_check_if_needed:250` | `router.py`, `base.py` | `test_router.py`, `test_health_check_timeout.py` | — |
| 6 | `ForgeClient._run()` sem timeout — forge CLI trava forever | `base.py:350-355` | 🔴 crítico | gh/glab lento → `proc.communicate()` bloqueia event loop indefinidamente | S | 1 arquivo, ~6 linhas: wrap + kill | `asyncio.wait_for(proc.communicate(), timeout=60)` + `proc.kill()` no `TimeoutError` | `base.py` | `test_forge_client.py` | — |
| 7 | Git `_run()` sem timeout — clone/fetch/pull trava forever | `worktree_manager.py:322-354` | 🔴 crítico | GitHub inacessível → `ensure_main()` trava event loop | S | 1 arquivo, ~6 linhas nos dois helpers | `asyncio.wait_for(proc.communicate(), timeout=60)` em `_git` e `_git_in_capture` | `worktree_manager.py` | `test_worktree_manager.py` | — |
| 8 | ERROR chunk no bot streaming — `error_type`/`error_message` sempre vazios | `agent_streaming.py:765-769` | 🔴 crítico | Bot recebe ERROR → type="" message="" — toda informação diagnóstica silenciosamente suprimida | S | 1 arquivo, 2 linhas: troca de `getattr(evt, ...)` por `evt.error_envelope.get(...)` | `envelope = (evt.error_envelope or {}); "type": envelope.get("error_type","") or "Error"; "message": envelope.get("message","")` | `agent_streaming.py` | `test_agent_streaming.py` | — |
| 9 | `cost_estimator.py` — sync I/O bloqueante em `async def _dispatch()` | `cost_estimator.py:72` | 🔴 crítico | Cada dispatch executa `open()` + `sqlite3.connect()` síncronos no event loop | M | Toca múltiplos arquivos (cost_estimator.py, usage_repository.py, implementer.py) com mudança de assinatura | Encapsular `check_stage_run()` em `await asyncio.to_thread(lambda: guard.check_stage_run())` no call site de `implementer.py:913` | `implementer.py`, `cost_estimator.py`, `usage_repository.py` | `test_cost_estimator.py`, `test_implementer.py` | — |
| 10 | `start_workflow_execution` retorna `total_steps: 0` | `workflow_executor.py:119-127` | 🟡 médio | Caller usa `total_steps` para progress — sempre vê 0, misleading para toda integração | S | 1 arquivo, 1 linha: reload ou `len(steps)` | Após loop de steps: `task_list = await self.task_manager.load_task_list(task_list.id) or task_list` | `workflow_executor.py` | `test_workflow_executor.py` | — |

---

## Plano de testes (top-10)

### Fix #1 — force-tick double-dispatch
- **Path**: `deile/tests/orchestration/pipeline/test_runner.py::test_force_tick_skips_when_in_flight`
- **O que prova**: `_force_tick_cb` chamado enquanto `_tick_in_flight=True` não cria nova Task — `asyncio.ensure_future` não é chamado. Também: `_force_tick_cb` chamado quando `_tick_in_flight=False` cria exatamente uma Task.

### Fix #2 — Validation gate history leak
- **Path**: `deile/tests/core/test_validation_gate.py::test_history_rollback_on_retry_exception`
- **O que prova**: Quando o retry provider lança `Exception`, `session.conversation_history` após `apply_validation_gate` tem exatamente o mesmo comprimento que antes da chamada — as duas entradas fantasma (assistant pré-gate + user gate_prompt) foram removidas.

### Fix #3 — Log deletado sem ledger
- **Path**: `deile/tests/infra/test_cli_worker_server.py::test_harvest_preserves_log_when_no_tokens`
- **O que prova**: Quando `has_tokens=False` e `task_id not in harvested`, o arquivo `.stdout.log` permanece no filesystem após o ciclo de harvest. O ledger não contém nenhuma entrada para o task_id.

### Fix #4 — Workflow loop trava
- **Path**: `deile/tests/orchestration/test_workflow_executor.py::test_wait_exits_promptly_on_infrastructure_error`
- **O que prova**: Quando `_execute_task_list_loop` lança `Exception` de infra, `wait_for_workflow_completion` retorna em menos de 5s com `has_failures=True` e `is_completed=False` — não espera o timeout de 1h.

### Fix #5 — Router health-check timeout
- **Path**: `deile/tests/core/models/test_router.py::test_health_check_timeout_does_not_block_provider_selection`
- **O que prova**: Um provider mock cujo `health_check()` nunca retorna faz `_health_check_if_needed()` completar dentro de 35s (timeout de 30s + margem), e o provider é marcado como unhealthy — `select_provider()` continua funcionando com os demais providers.

### Fix #6 — ForgeClient timeout
- **Path**: `deile/tests/orchestration/forge/test_forge_client.py::test_run_kills_subprocess_on_timeout`
- **O que prova**: `ForgeClient._run()` com subprocesso que bloqueia indefinidamente lança `asyncio.TimeoutError` dentro do tempo configurado e o processo filho é terminado (`proc.returncode` não é None após o kill).

### Fix #7 — WorktreeManager timeout
- **Path**: `deile/tests/orchestration/pipeline/test_worktree_manager.py::test_git_helper_raises_on_timeout`
- **O que prova**: `_git('clone', ...)` com subprocesso que bloqueia além do timeout lança `WorktreeError` com mensagem clara e o processo filho é terminado — `ensure_main()` propaga a exceção em vez de travar.

### Fix #8 — ERROR chunk envelope
- **Path**: `deile/tests/core/test_agent_streaming.py::test_error_chunk_carries_envelope_fields`
- **O que prova**: Quando o stream emite `UnifiedStreamEvent(type=ERROR, error_envelope={"error_type": "BudgetExceeded", "message": "Limite atingido"})`, o `StreamChunk` resultante tem `payload["type"] == "BudgetExceeded"` e `payload["message"] == "Limite atingido"` — não strings vazias.

### Fix #9 — cost_estimator sync I/O
- **Path**: `deile/tests/orchestration/pipeline/test_implementer.py::test_dispatch_does_not_block_event_loop`
- **O que prova**: `_dispatch()` com `asyncio.to_thread` correto libera o event loop durante a estimativa de custo — um segundo coroutine de alta prioridade completando ao mesmo tempo demonstra que o loop não ficou bloqueado (medindo latência de segunda task < 50ms durante a estimativa).

### Fix #10 — total_steps zero
- **Path**: `deile/tests/orchestration/test_workflow_executor.py::test_start_workflow_returns_correct_total_steps`
- **O que prova**: `start_workflow_execution(objective="...", steps=[...5 steps...])` retorna dict com `total_steps == 5` e `execution_info["total_tasks"] == 5` — não zero.

---

## Incerteza & Gaps

| Fix # | Confiança | Riscos residuais |
|-------|-----------|-----------------|
| 1 | média | A flag boolean `_tick_in_flight` é simples mas frágil: se `tick()` lançar exceção antes do `try` (raro), a flag fica True permanentemente. A abordagem robusta (wake-flag + `asyncio.Event`) toca mais código. Risco de starvation se ticks forçados são frequentes. |
| 2 | alta | Risco residual mínimo. O slice `del session.conversation_history[_history_checkpoint:]` é a operação inversa exata das duas appends. Nenhum outro código lê a lista entre os dois appends e o rollback no except. |
| 3 | alta | A condição exata do guard deve ser verificada para o caso `task_id in harvested AND has_tokens=False` — nesse caso a deleção é legítima (já contabilizado). A proposta cobre o caso crítico; o caso edge (already-harvested com zero tokens) pode precisar de tratamento explícito. |
| 4 | alta | Requer que `mark_task_completed` funcione mesmo no estado de erro (ex: se o erro foi na própria conexão SQLite). Uma variação mais segura seria registrar o erro em memória e persistir na próxima oportunidade. |
| 5 | média | O timeout de 30s é arbitrário. Se um provider legítimo é lento (ex: cold start), será incorretamente marcado unhealthy. Deve ser configurável via `provider_config.timeout_seconds` (após fix #2 que ainda não o consome). O catch deve diferenciar `TimeoutError` de outros erros. |
| 6 | alta | `asyncio.wait_for(proc.communicate(), timeout=N)` pode deixar o subprocesso em estado inconsistente se o processo não responder a SIGTERM. O `proc.kill()` + `await proc.wait()` no handler deve usar `SIGKILL` como fallback após breve espera. |
| 7 | alta | Mesmo que #6: SIGKILL como fallback. O timeout de git clone deve ser maior (120s) vs fetch/pull (30s). |
| 8 | alta | O `or "Error"` padrão no tipo de erro é razoável mas pode mascarar tipos novos que chegam vazios por bug distinto. Monitorar se `error_type` ausente no envelope se torna frequente. |
| 9 | baixa | Tornar `check_stage_run()` async requer mudanças em cadeia em `StageBudgetGuard`, `cost_estimator.py` e `usage_repository.py`. O wrap via `asyncio.to_thread(lambda: guard.check_stage_run())` é mais seguro mas impede propagação correta de exceções async. Requer teste de regressão abrangente da cadeia de budget. |
| 10 | alta | O reload via `load_task_list(task_list.id)` faz uma query extra ao SQLite — overhead aceitável dado que é apenas 1 query por workflow criado. Alternativa (usar `len(steps)`) é mais simples mas contorna o bug estrutural em vez de corrigi-lo. |

---

## Descartados

### D-1 — `gemini_provider.py:772-804` — TOCTOU em `create_chat_session()`
**Razão do descarte**: Não há `await` points entre o check `if session_id in self._chat_sessions` (linha 772) e o store `self._chat_sessions[session_id] = chat` (linha 804). `_get_tools_for_generate_content()` e `self.client.chats.create()` são métodos síncronos. Em asyncio, nenhuma suspensão de contexto pode ocorrer entre dois statements não-await, portanto duas corrotinas não podem intercalar entre as linhas 772 e 804.

### D-2 — `router.py:266-271` — singleton `_model_router` sem lock
**Razão do descarte**: `get_model_router()` é função síncrona. Em CPython asyncio (single-thread), funções síncronas não são preemptadas por outras corrotinas. A arquitetura DEILE é async-first com single-thread event loop (CLAUDE.md). Sem threading paralelo, a inicialização é atômica do ponto de vista do event loop.

### D-3 — `base.py:540-543` — `_update_stats()` sem lock em incrementos
**Razão do descarte**: `_update_stats` é método síncrono chamado após um `await` retornar. Em asyncio single-thread, nenhum context switch ocorre durante execução síncrona. `+=` em CPython também é internamente atômico ao nível de bytecode via GIL. Nenhuma race real é possível.

### D-4 — `gemini_provider.py:257` — `_chat_sessions` cresce sem bound
**Razão do descarte**: Todos os callers de `create_chat_session()` dentro do arquivo usam chaves efêmeras prefixadas com uuid que são `pop()`adas após uso (linhas 574/622/975/981). Nenhum caller externo cria sessão persistente sem limpeza. O dict não cresce sem bound na prática.

### D-5 — `base.py:583-586` — `except Exception: pass` no callback `on_label_change`
**Razão do descarte**: O silenciamento de exceções é intencional e documentado: o callback `on_label_change` é opcional (`Optional[Callable]`) e seu papel é de observer, não caminho crítico. Uma falha no observer não deve desfazer uma transição de label que foi aplicada com sucesso. Mesmo padrão em `gitlab_forge.py:1019,1102`.

### D-6 — `detection.py:29-30` — `_probe_cache` sem TTL
**Razão do descarte**: Apenas resultados bem-sucedidos (não-None) são escritos no cache (linhas 112 e 292 ambas verificam `if result is not None`). Uma probe com falha nunca é cacheada, portanto será reexecutada na próxima call. O cache permanente para identificações bem-sucedidas é intencional — o tipo de host forge não muda em runtime.

### D-7 — `cli_renderer.py:225-228` — interpolação de `issue_template` e `main` em string glab
**Razão do descarte**: Os comandos renderizados por `cli_renderer.py` são snippets de texto injetados no worker brief prompt, não executados por shell pelo pipeline em si. O worker (claude -p ou deile-worker) os executa no seu próprio sandbox. `issue_template` e `main` são controlados pela configuração interna do pipeline, não são texto livre do usuário.

### D-8 — `briefs.py:1-880` — varredura completa, sem bugs encontrados
**Razão do descarte**: Todo acesso a `_MENTION_ACTION_TEMPLATES` passa por `_classify_mention_action` que tem branch default retornando `'default'`. Todos os `.format(**params)` em `_render_brief` usam params montados por `_build_brief_params` que cobre todos os placeholders. Código é bem-estruturado com defaults seguros.

### D-9 — `notifier.py:74` — lazy singleton `_DM_FN` sem lock
**Razão do descarte**: Python asyncio é single-thread. Entre o check (linha 74) e a assignment (linha 75) não há `await` — nenhuma outra corrotina pode executar. GIL protege contra race de threads. `_resolve_dm_function()` é idempotente (importa função de módulo — sempre o mesmo objeto).

### D-10 — `gc.py:85-186` — TOCTOU entre fetch e mutação de labels
**Razão do descarte**: Intencional e documentado na docstring das linhas 105-108: "second returns success rather than noop because its API calls receive silent 404s (github_forge.py:977-978)". A função é projetada para ser idempotente e trata invocações concorrentes explicitamente via 404s silenciosos.

### D-11 — `claude_dispatcher.py:1-274` — varredura completa, sem bugs encontrados
**Razão do descarte**: Timeout handling correto em `claude_dispatcher.py:115-117` com `except asyncio.TimeoutError: proc.kill(); await proc.wait()`. Este arquivo é inclusive a referência correta para o fix do bug #19.

### D-12 — `cli_worker_server.py:562-564` — `os.environ` mutado sem lock em dispatches concorrentes
**Razão do descarte**: Para um pod de um único kind, `_worker_home(adapter)` retorna valor constante (`/home/<kind>` ou env var fixo), portanto todos os dispatches concorrentes computam dicts `overlay` idênticos. As escritas em `os.environ` são races idempotentes — múltiplas corrotinas escrevem os mesmos pares chave-valor. Nenhum estado incorreto pode resultar.

### D-13 — `aio_fileio.py:43-45` — `write_json` não-atômica (sem tmp+replace)
**Razão do descarte**: Comportamento não-atômico explicitamente documentado na docstring: `"Async-safe json.dump (indent=2, UTF-8) — non-atomic."`. É uma decisão de design intencional. Callers que precisam de atomicidade implementam próprio tmp+replace, como visto em `cli_worker_server.py:803-807`.

### D-14 — `logs.py:46-88` — `_ensure_initialized()` double-init sem lock
**Razão do descarte**: DEILE é async-first com single event loop. `_ensure_initialized()` é chamado na primeira chamada de `get_logger()`, que em prática ocorre no thread principal antes de qualquer worker `asyncio.to_thread`. O módulo `logging` do Python tem locking interno em `Logger.addHandler`. A guard `if not logger.handlers:` (linha 52) mitiga a maioria dos casos. Sem impacto funcional demonstrável.

### D-15 — `orchestrator.py:255-266` — check-then-acquire em `lock.locked()` → `await lock.acquire()`
**Razão do descarte**: Em asyncio cooperativo, quando `asyncio.Lock` está livre, `await lock.acquire()` retorna True imediatamente sem suspender a corrotina atual. Portanto nenhuma outra corrotina pode executar entre o check `lock.locked()` (linha 256) e o acquire. O comentário no código (linhas 246-254) documenta corretamente esse raciocínio.

### D-16 — `sqlite_task_manager.py:224-232` — `_ensure_schema` lazy-init do `_init_lock`
**Razão do descarte**: Não há `await` entre `if self._init_lock is None:` (linha 226) e `self._init_lock = asyncio.Lock()` (linha 227) — scheduling cooperativo previne interleaving. O double-check dentro do `async with` (linhas 229-230) provê a guarda real de idempotência para o schema init.

### D-17 — `model_resolver.py:121-127` — `os.environ.get()` direto em vez de `get_settings()`
**Razão do descarte**: Desvio arquitetural intencional com justificativa explícita na docstring (linhas 94-107): a camada de settings valida `DEILE_PIPELINE_MODEL_<STAGE>` contra regex de provider:model e rejeitaria IDs de modelo CLI de forma livre. `os.environ` é lido diretamente por necessidade documentada, não por omissão.

---

## Follow-up

Issue de follow-up criada: **[#777 — [FIX] Correções top-10 da auditoria #776](https://github.com/elimarcavalli/deile/issues/777)** — checklist agregado com os 10 fixes priorizados, cada item com `arquivo:linha`, blast-radius, severidade e path de teste.
