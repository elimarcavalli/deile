# Runbook E2E — Harness project-agnostic (issue #612, AC-1 / AC-2)

> **Escopo deste documento.** A issue #612 desacopla o harness/pipeline do repo
> hardcoded `elimarcavalli/deile`. A fatia de **código** (AC-3 a AC-7) foi
> entregue e coberta por testes automatizados. Os critérios **AC-1** (E2E
> GitHub) e **AC-2** (E2E GitLab) exigem repositórios externos reais +
> credenciais e **não são unit-testáveis** — este runbook é o passo manual
> remanescente, para o Humano executar e anexar evidência à issue.

## O que mudou no código (contexto para a validação)

| AC | Mudança | Onde |
|---|---|---|
| AC-3 | `resolve_forge_repo()` **falha alto** (`ConfigurationError`) quando nenhum repo está configurado — sem fallback silencioso para `elimarcavalli/deile`. Surfaces graciosas (painel/CLI) degradam com `WARNING`. | `deile/orchestration/pipeline/constants.py` |
| AC-3 | Default de `Settings.pipeline_repo` passou de `"elimarcavalli/deile"` para `""`. | `deile/config/settings.py` |
| AC-4/5 | Repo-alvo vem de **fonte única**: a chave discreta `pipeline.repo` no ConfigMap `deile-runtime-config`. Os manifests 46/47/55 a referenciam via `configMapKeyRef`; o pod `deile-pipeline` recebe `DEILE_FORGE_REPO` dela. | `infra/k8s/manifests/46,47,55-*.yaml`, `infra/k8s/_panel*.py`, `infra/k8s/_setup.py` |
| AC-6/7 | Testes de fiação exercitam o call-site real (implementer, monitor tick, painel). | `deile/tests/orchestration/pipeline/test_forge_repo_injection.py`, `deile/tests/infra/test_panel_data.py`, `deile/tests/infra/test_monitor_tick.py` |

**Consequência operacional:** subir a stack sem `pipeline.repo` no ConfigMap (e
sem `DEILE_FORGE_REPO`) faz o `deile-pipeline` abortar no startup com mensagem
clara — comportamento desejado. O deploy de referência do próprio DEILE continua
funcionando porque o ConfigMap traz `pipeline.repo: elimarcavalli/deile`.

## Como apontar o harness para QUALQUER repo (sem editar código)

Edite **uma única chave** no ConfigMap e reinicie a stack:

```bash
K=~/.rd/bin/kubectl

# GitHub: owner/repo
$K -n <ns> patch configmap deile-runtime-config \
  --type merge -p '{"data":{"pipeline.repo":"<owner>/<repo>","forge.kind":"github"}}'

# GitLab: group/(subgroup/)*project + forge.kind=gitlab (override canônico)
$K -n <ns> patch configmap deile-runtime-config \
  --type merge -p '{"data":{"pipeline.repo":"<group>/<project>","forge.kind":"gitlab"}}'

python3 infra/k8s/deploy.py -n <ns> k8s restart
```

> Namespaces criados via `python3 infra/k8s/deploy.py k8s create-namespace` já
> perguntam forge/repo no fluxo interativo e gravam a chave `pipeline.repo`
> automaticamente (`infra/k8s/_setup.py`).

---

## AC-1 — E2E GitHub (repo neutro ≠ `elimarcavalli/deile`)

**Pré-requisitos**
- Um repositório GitHub sandbox dedicado (ex.: `<sua-org>/deile-harness-sandbox`),
  vazio ou com um esqueleto mínimo, e um PAT (`GITHUB_TOKEN`) com escopo
  `repo`.
- Cluster DEILE de pé (`python3 infra/k8s/deploy.py k8s up`), claude-worker
  logado (`k8s claude-login`) se for despachar para o claude-worker.

**Passos**
1. Provisione o token e o repo-alvo:
   ```bash
   K=~/.rd/bin/kubectl
   $K -n <ns> patch secret deile-secrets \
     -p '{"stringData":{"GITHUB_TOKEN":"<PAT>"}}'
   $K -n <ns> patch configmap deile-runtime-config \
     --type merge -p '{"data":{"pipeline.repo":"<org>/<repo>","forge.kind":"github"}}'
   python3 infra/k8s/deploy.py -n <ns> k8s restart
   ```
2. Confirme que o pipeline subiu apontando para o repo-alvo (não o default):
   ```bash
   $K -n <ns> logs deploy/deile-pipeline --tail=80 | grep -i "repo="
   # esperado: starting pipeline monitor (repo=<org>/<repo>, ...)
   ```
3. Abra uma issue simples no repo-alvo (via UI ou `gh`) e adicione
   `~workflow:nova`. Acompanhe o ciclo completo
   `classify → refine → implement → pr_review → follow_ups`.
4. **Evidência a anexar na #612:**
   - Trecho do log do `deile-pipeline` mostrando `repo=<org>/<repo>`.
   - Link da issue e da PR criadas **no repo-alvo** (não em `elimarcavalli/deile`).

**Critério de sucesso:** issue percorre o pipeline e gera PR no repo-alvo,
configurado **apenas** via `pipeline.repo`/`forge.kind` — zero edição de código.

---

## AC-2 — E2E GitLab (`DEILE_FORGE_KIND=gitlab`)

**Pré-requisitos**
- Um projeto GitLab (cloud ou self-hosted) e um PAT (`GITLAB_TOKEN`/`GL_TOKEN`)
  com escopos `api`, `read_repository`, `write_repository`.

**Passos**
1. Provisione token + repo + forge:
   ```bash
   K=~/.rd/bin/kubectl
   $K -n <ns> patch secret deile-secrets \
     -p '{"stringData":{"GITLAB_TOKEN":"<PAT>"}}'
   $K -n <ns> patch configmap deile-runtime-config \
     --type merge -p '{"data":{"pipeline.repo":"<group>/<project>","forge.kind":"gitlab"}}'
   # self-hosted: defina também DEILE_GITLAB_HOST no manifest/ConfigMap.
   python3 infra/k8s/deploy.py -n <ns> k8s restart
   ```
2. Confirme o startup (`repo=<group>/<project>`, forge gitlab).
3. Abra uma issue no projeto GitLab, marque `~workflow:nova`, acompanhe o ciclo
   completo (issue → MR → merge), cobrindo a divergência de API GH↔GL.
4. **Evidência a anexar na #612:** log do pipeline + link da issue e da MR no
   projeto GitLab.

**Critério de sucesso:** mesmo fluxo do AC-1, agora contra GitLab.

> **Downgrade documentado (anti-flood):** se a paridade GitLab inviabilizar o
> E2E completo no momento da validação, registre AC-2 como **follow-up DESTA
> issue** (não abrir nova) — a fatia de código já é forge-agnóstica e os testes
> de fiação cobrem ambos os caminhos.

---

## Categoria D — `DEILEBOT_REPO` (decisão registrada)

`infra/setup_environment.py:43` mantém `DEILEBOT_REPO =
"https://github.com/elimarcavalli/deilebot.git"`. **Decisão: N/A — fora do
pipeline.** É o install opcional do bot (extra `[bot]`), não faz parte do fluxo
project-agnostic do harness; permanece apontando para o repo do produto por
design (mesma natureza da Categoria C — self-identity do DEILE).
