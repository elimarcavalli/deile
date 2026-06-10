# `infra/k8s/manifests/_README.md` — notas sobre manifests específicos

> Este arquivo documenta instruções manuais que não cabem em manifest YAML
> (ex: comandos `kubectl patch`). Consulte-o antes de aplicar manifests
> individualmente.

---

## Política: Secrets de Bearer não têm manifest stub (issue #356)

Os Secrets de bearer token (`worker-bearer`, `claude-worker-bearer`,
`pipeline-status-bearer`) são criados **programaticamente** pelo `deploy.py`
— nunca via `kubectl apply -f <manifest stub>`. O motivo é evitar a race
condition em que o pod sobe com Secret vazio antes que o token real seja
populado.

| Secret | Criado por | Quando |
|--------|-----------|--------|
| `worker-bearer` | `deploy.py k8s up` | bootstrap inicial |
| `claude-worker-bearer` | `deploy.py k8s claude-login` → `_kubectl_sync_bearer_token()` | **antes** do Deployment 50 ser aplicado |
| `pipeline-status-bearer` | `deploy.py k8s up` | bootstrap inicial |

Para recriar `claude-worker-bearer` manualmente (sem `claude-login`):

```bash
kubectl create secret generic claude-worker-bearer \
  --from-literal=CLAUDE_WORKER_BEARER_TOKEN=<token> \
  -n deile --dry-run=client -o yaml | kubectl apply -f -
```

---

## Forge tokens (`GITLAB_TOKEN` no `deile-secrets`)

> ⚠️ **Obsoleto a partir de #354** — o `k8s up` agora propaga `GITLAB_TOKEN`
> (e o alias `GL_TOKEN`) para o Secret `deile-secrets` automaticamente.
> Preservado como referência histórica.

DEILE consome tokens de forge via o Secret `deile-secrets` (montado em
`/run/secrets/deile` em cada pod). Para habilitar GitLab, adiciona-se
**uma** chave nova: `GITLAB_TOKEN`. O `wrapper.py` já carrega todo
arquivo em `/run/secrets/deile/` como env var, então nenhuma mudança de
manifest é necessária — o payload do Secret decide quais forges estão
ativas.

- **GitHub-only** → popule `GITHUB_TOKEN` no `deile-secrets`.
- **GitLab-only** → popule `GITLAB_TOKEN` no `deile-secrets`.
- **Dual-forge**  → popule **AMBOS** no `deile-secrets`.

O pod do pipeline recusa iniciar se nenhum token estiver presente.

### Adicionar (ou rotacionar) o token GitLab sem tocar no GitHub

```bash
GL_PAT=$(read -sp 'GitLab PAT: ' x && echo "$x")
kubectl -n deile patch secret deile-secrets \
  -p "{\"stringData\":{\"GITLAB_TOKEN\":\"${GL_PAT}\"}}"
```

### Scopes necessários

| Token | Scopes |
|---|---|
| `GITHUB_TOKEN` | `repo` (full); `workflow` se o pipeline rotular PRs. |
| `GITLAB_TOKEN` | `api`, `read_repository`, `write_repository`. |

---

## HPA ConfigMap sync

O `HorizontalPodAutoscaler` em `infra/k8s/manifests/46-deile-worker-hpa.yaml` não re-checa o `ConfigMap` `deile-runtime-config` ao vivo, então qualquer mudança nos parâmetros `worker.hpa.*` precisa ser propagada manualmente antes de aplicar a nova HPA.

Use `scripts/update_hpa.sh` para ler os campos `worker.hpa.minReplicas`, `worker.hpa.maxReplicas` e `worker.hpa.targetAverageValue` em `infra/k8s/manifests/47-deile-runtime-config.yaml`, validar as invariantes (`minReplicas <= maxReplicas` e `maxReplicas >= 2`) e executar o `kubectl patch hpa deile-worker ...` com os novos valores. O script aceita `--config-file`, `--namespace`, `--hpa-name` e `--kubectl` para acomodar ambientes diferentes e oferece `--dry-run` para testar a payload com `kubectl --dry-run=client` antes de aplicar.

Exemplo mínimo (sempre rode após editar o ConfigMap):

```bash
scripts/update_hpa.sh --namespace deile --config-file infra/k8s/manifests/47-deile-runtime-config.yaml
```

Se o script abortar, corrija primeiro as chaves `worker.hpa.*` (o `maxReplicas` precisa ser pelo menos `2`, e `minReplicas` não pode ultrapassar `maxReplicas`).
