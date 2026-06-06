# OpenRouter no DEILE — provider unificador (uma chave → todos os modelos) — Plano

> **Para workers agênticos:** SUB-SKILL — `superpowers:subagent-driven-development` ou `executing-plans`. Passos em checkbox.
> **Relacionado:** este doc é a **Parte 3 extraída** do plano `2026-06-06-multi-cli-worker-fleet.md`. É independente e pode ser feito antes/sem a frota multi-CLI.

**Goal:** registrar **OpenRouter** como provider no DEILE para que o **deile-worker in-process** e o **DEILE CLI local** falem qualquer modelo (DeepSeek, Qwen, Gemini, Claude, GPT, Llama…) através de **uma única chave** (`OPENROUTER_API_KEY`) e cobrança única pay-per-token competitiva. Serve também de provider unificador para os CLI workers da frota.

**Por que:** a cobrança separada de uso programático da Anthropic (15/jun/2026) torna urgente rotear o grosso do pipeline para modelos baratos. OpenRouter dá DeepSeek (~$0,78/M out), Qwen, Gemini Flash etc. sob a mesma chave/billing, com fallback e teto de gasto por chave. Reduz o egress da frota a um host só (`openrouter.ai`).

**Tech Stack:** DEILE provider layer (`deile/core/models/`), `model_providers.yaml`, `bootstrap_providers()`, K8s Secret. OpenRouter é **OpenAI-compatible** (`/api/v1`), então reusa o adapter OpenAI existente.

---

## PARTE 0 — Como o DEILE registra providers hoje (estudo)
- `deile/config/model_providers.yaml` — seção `providers:` (base_url, api_key_env, etc.) + seção `models:` (slug, tier, provider). Fonte única de verdade dos modelos/tiers.
- `deile/core/models/bootstrap.py::bootstrap_providers(router=...)` — registra cada provider no `ModelRouter` (singleton) conforme as chaves de API presentes (registro condicional: só registra se a env key existe). É o que `deile.py` e o deile-worker chamam no startup.
- Adapters concretos em `deile/core/models/` (Anthropic, OpenAI, DeepSeek, Google). **OpenRouter NÃO precisa de adapter novo** — é OpenAI-compatible; reusa o adapter OpenAI apontando `base_url` para `https://openrouter.ai/api/v1`.
- Seleção por-stage no pipeline (`model_resolver`) já aceita `provider:model` — OpenRouter entra como `openrouter:<model>`.

> **Verificar no impl (context7/SDK):** confirmar que o adapter OpenAI do DEILE aceita `base_url` custom + `api_key` de env distinta. Se o adapter OpenAI hardcoda `OPENAI_API_KEY`/endpoint, generalizar para aceitar `base_url`+`api_key_env` parametrizados (provável pequena mudança).

---

## PARTE 1 — Design

### 1.1 Registro do provider OpenRouter
`model_providers.yaml`, seção `providers:`:
```yaml
  openrouter:
    type: openai_compatible        # reusa o adapter OpenAI
    base_url: "https://openrouter.ai/api/v1"
    api_key_env: "OPENROUTER_API_KEY"
    # headers opcionais (best-effort; OpenRouter usa p/ ranking/atribuição, não obrigatórios)
    extra_headers:
      HTTP-Referer: "https://github.com/elimarcavalli/deile"
      X-Title: "DEILE"
```

### 1.2 Catálogo de modelos (curado, com tier/custo)
`model_providers.yaml`, seção `models:` — adicionar os que interessam, com **custo documentado** para decisão consciente por-stage:
```yaml
  - slug: "openrouter:deepseek/deepseek-chat"     # ~$0.28/M in, ~$0.88/M out (varia)
    provider: openrouter
    tier: cheap
  - slug: "openrouter:deepseek/deepseek-reasoner" # reasoning barato
    provider: openrouter
    tier: cheap
  - slug: "openrouter:qwen/qwen3-coder"           # coder open-weights, MoE 480B
    provider: openrouter
    tier: cheap
  - slug: "openrouter:google/gemini-2.5-flash"    # rápido/barato
    provider: openrouter
    tier: cheap
  - slug: "openrouter:anthropic/claude-sonnet-4.6"# premium via OpenRouter
    provider: openrouter
    tier: premium
  - slug: "openrouter:openai/gpt-5.5"             # premium via OpenRouter
    provider: openrouter
    tier: premium
```
> Os custos exatos mudam — a tabela canônica de custo do DEILE (`infra/k8s/jsonl_cost.py` / `UsageRepository`) deve ganhar entradas para os modelos OpenRouter usados, OU confiar no `total_cost` reportado pela API OpenRouter (ela retorna `usage` com custo). **Decisão de impl:** preferir o custo reportado pela resposta do OpenRouter (campo `usage`) quando disponível, com a tabela como fallback.

### 1.3 bootstrap_providers
Em `bootstrap.py`: se `OPENROUTER_API_KEY` presente → registrar o provider `openrouter` no router (adapter OpenAI com `base_url`+`api_key`+`extra_headers` do YAML). Registro condicional como os demais.

### 1.4 Secret + propagação (segredo do operador — só Secret, nunca no repo)
- `.env` (gitignored): `OPENROUTER_API_KEY=...`.
- `deploy.py k8s up`: propagar `OPENROUTER_API_KEY` para o Secret `deile-secrets` (igual aos outros `*_API_KEY`) + montar como env no **deile-worker** (e no DEILE CLI local lê do `.env`).
- **NUNCA** hardcodar a chave; só o **nome** da env var aparece no código/manifests.

### 1.5 Roteamento por-stage barato (uso prático)
Com o provider registrado, os per-stage models aceitam `openrouter:deepseek/deepseek-chat` etc.:
```bash
DEILE_PIPELINE_MODEL_CLASSIFY=openrouter:deepseek/deepseek-chat
DEILE_PIPELINE_MODEL_REFINE=openrouter:qwen/qwen3-coder
DEILE_PIPELINE_MODEL_IMPLEMENT=openrouter:anthropic/claude-sonnet-4.6   # premium só onde precisa
```
Painel (`StageModelsView`/`DispatchMatrixView`) passa a listar os modelos OpenRouter no picker (vêm do `model_providers.yaml`).

---

## PARTE 2 — Task breakdown (TDD)
- [ ] **OR1** Generalizar (se preciso) o adapter OpenAI do DEILE p/ aceitar `base_url`+`api_key_env`+`extra_headers` parametrizados. **Teste:** unit — adapter aponta p/ base_url custom + manda a key/headers certos (mock HTTP). Consultar SDK via context7 antes.
- [ ] **OR2** Adicionar provider `openrouter` + catálogo de modelos em `model_providers.yaml`. **Teste:** loader parseia; modelos aparecem no inventário.
- [ ] **OR3** `bootstrap_providers`: registro condicional do openrouter quando `OPENROUTER_API_KEY` presente. **Teste:** com a env → provider registrado; sem → ignorado (0 providers extra).
- [ ] **OR4** `deploy.py k8s up`: propagar `OPENROUTER_API_KEY` p/ `deile-secrets` + env no deile-worker. **Teste:** dry-run mostra a env no manifest renderizado.
- [ ] **OR5** Custo: usar `usage`/custo reportado pela resposta OpenRouter; fallback na tabela. **Teste:** parse de uma resposta OpenRouter real capturada → custo extraído.
- [ ] **OR6** Painel: modelos OpenRouter no picker por-stage. **Teste:** picker lista os slugs `openrouter:*`.
- [ ] **OR7** Smoke E2E (custo mínimo): 1 mensagem via `openrouter:deepseek/deepseek-chat` pelo deile-worker, confirmar resposta + custo registrado. Doc + `DECISOES.md`.

---

## PARTE 3 — Revisão cética
1. **Adapter OpenAI pode hardcodar endpoint/key** → OR1 generaliza; se já aceita base_url, é só config. Verificar com SDK (context7) antes de assumir.
2. **Custo:** a tabela `jsonl_cost.py` é específica de Claude (claude -p). Para OpenRouter (deile-worker in-process), o custo vem do `UsageRepository` do DEILE — confirmar que ele cobre modelos OpenAI-compat e que o preço por modelo OpenRouter está correto (ou usar o `usage.cost` da resposta). Risco de undercount como já houve com Claude.
3. **Rate-limit/erros OpenRouter** diferem por modelo (alguns modelos do OpenRouter caem/variam) → tratar 429/503 com retry/backoff (o DEILE já tem circuit breaker por provider — confirmar que cobre o openrouter).
4. **Privacidade:** OpenRouter roteia p/ provedores terceiros — ok para código do projeto, mas ciente de que dados passam por mais um hop. Documentar.
5. **`provider:model` com 2 barras:** `openrouter:anthropic/claude-sonnet-4.6` tem `:` e `/` — confirmar que o parser de slug do `model_resolver` aceita (o validator `^[a-z][a-z0-9_-]*:[a-z0-9._/-]+$` precisa permitir `/` no lado do modelo). **Ajuste provável no regex.**

---

## Ordem
OR1(adapter) → OR2/OR3(registro) → OR4(secret) → OR5(custo) → OR6(painel) → OR7(E2E+docs). Independente da frota multi-CLI; pode ser o **primeiro passo barato** (habilita rotear o deile-worker atual p/ DeepSeek sem nenhum worker novo).
