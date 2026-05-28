# `infra/k8s/manifests/_README.md` — notas sobre manifests específicos

> Este arquivo documenta instruções manuais que não cabem em manifest YAML
> (ex: comandos `kubectl patch`). Consulte-o antes de aplicar manifests
> individualmente.

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
