# `infra/k8s/` — DEILE + deilebot em containers (Rancher Desktop / k3s)

> **Modelo conceitual** vive em
> [`docs/system_design/14-CONTAINERIZACAO.md`](../../docs/system_design/14-CONTAINERIZACAO.md).
> Aqui você encontra **o passo-a-passo de operação** — instalar
> ferramentas, configurar segredos, build da imagem, deploy, teste e
> troubleshooting.

---

## 1. Pré-requisitos — instalando o ambiente do zero (macOS)

### 1.1 Rancher Desktop (k3s + containerd)

[Rancher Desktop](https://rancherdesktop.io/) sobe um k3s local
embarcado com `kube-router` (que enforce NetworkPolicy — essencial pro
nosso modelo).

```bash
brew install --cask rancher
open -a "Rancher Desktop"
```

Na primeira tela:

- **Container Engine**: escolha **`containerd (nerdctl)`** — não Docker.
  k3s lê imagens da mesma store do containerd quando você builda em
  `--namespace k8s.io`.
- **Kubernetes**: habilitado, versão `≥ 1.28`.
- **Path Setup**: deixe o instalador adicionar `~/.rd/bin/` ao PATH.

Verifique:

```bash
~/.rd/bin/kubectl get nodes
# NAME                   STATUS   ROLES                  AGE   VERSION
# lima-rancher-desktop   Ready    control-plane,master   …     v1.28.x+k3s1

~/.rd/bin/nerdctl --namespace k8s.io info | head -3
```

### 1.2 Bot Discord (token + permissões)

1. <https://discord.com/developers/applications> → **New Application**.
2. **Bot** → copie o token (esse vai para `DEILE_BOT_DISCORD_TOKEN`).
3. **Privileged Gateway Intents** — habilite os três:
   `PRESENCE`, `SERVER MEMBERS`, `MESSAGE CONTENT`.
4. **OAuth2 → URL Generator**:
   - Scopes: `bot` + `applications.commands`.
   - Bot Permissions (mínimo): `Send Messages`, `Read Message History`,
     `Add Reactions`, `Use Slash Commands`.
5. Abra a URL gerada e convide o bot para o seu servidor.

Anote o **seu** `user_id` (clique direito → Copy User ID, ative
`Developer Mode` em Settings/Advanced). Esse vai em `owners:` no
`bot-config` (ver §3.3).

### 1.3 `.env` no repo

Crie/edite o `.env` da raiz do repo deile:

```ini
# Pelo menos UMA chave LLM:
ANTHROPIC_API_KEY=sk-ant-...
# OPENAI_API_KEY=sk-...
# DEEPSEEK_API_KEY=sk-...
# GOOGLE_API_KEY=AIza...

DEILE_BOT_DISCORD_TOKEN=MTQ5...  # do passo 1.2
# DEILE_BOT_AUTH_TOKEN não precisa — run.sh gera um a cada `up`
```

> `.env` está no `.dockerignore` (`infra/k8s/.dockerignore`) — **não
> entra na imagem**. Os Secrets do K8s são criados em runtime a partir
> dele e nunca aparecem em `kubectl get secret -o yaml` por acidente.

---

## 2. Build e deploy — caminho feliz

```bash
bash infra/k8s/run.sh build   # ~5–10 min na 1ª vez; cache nas próximas
bash infra/k8s/run.sh up      # namespace + NPs + Secrets + bot Deployment
bash infra/k8s/run.sh test    # cria o Job de prova → DM no Discord
```

Sucesso = DM aparece no Discord (no canal direto entre você e o bot)
e o Job termina `1/1 completions in XXs`. Logs:

```bash
bash infra/k8s/run.sh logs    # bot + Job
```

Quando terminar e quiser limpar tudo:

```bash
bash infra/k8s/run.sh down    # kubectl delete ns deile
```

---

## 3. Manifests, um por arquivo

```
infra/k8s/
├── .dockerignore                 ← nada secreto, nada grande entra na imagem
├── Dockerfile                    ← multi-stage; non-root; tini; readonly-friendly
├── wrapper.py                    ← entry-point que lê /run/secrets/<role>/, popa env
├── run.sh                        ← orquestrador build/up/test/logs/down
├── README.md                     ← este arquivo
└── manifests/
    ├── 00-namespace.yaml         namespace `deile` w/ PSS:restricted
    ├── 15-bot-config.yaml        ConfigMap: deilebot.yaml com owners
    ├── 20-bot-deployment.yaml    bot Deployment + Service (ClusterIP :8765)
    ├── 30-deile-job.yaml         one-shot deile Job (proof-of-DM)
    ├── 35-deile-interactive.yaml long-running deile-shell (`kubectl exec`)
    ├── 40-network-policy.yaml    default-deny + selective allow
    └── 99-deile-debug.yaml       probe Pod (manual; off-path)
```

### 3.1 Workflow

1. `00-namespace.yaml` cria o namespace com **Pod Security
   `restricted`** ligado em `enforce`. Pods que pedirem privilégio
   demais são rejeitados no admission.
2. `40-network-policy.yaml` aplica **default-deny** (todo ingress/egress
   bloqueado) e depois libera só:
   - DNS para kube-system (53/UDP+TCP);
   - bot 8765/TCP ingress só de pods `role=deile`;
   - `role=deile` egress 8765 para `app=deilebot`;
   - `role=deile` egress 443 a `0.0.0.0/0 except RFC1918` (Anthropic etc.);
   - `app=deilebot` egress 443/80 a `0.0.0.0/0 except RFC1918`
     (discord.com).
3. `run.sh up` cria dois Secrets a partir do `.env`:
   - `bot-secrets`: Discord token + Bearer auth + chaves LLM (bot
     precisa pra agente embutido responder DMs);
   - `deile-secrets`: chaves LLM + Bearer (deile precisa pra LLM e
     pra falar com bot).
4. `15-bot-config.yaml` é um ConfigMap com `deilebot.yaml` — define
   `owners:` (no formato `<provider>:<provider_user_id>` para
   sobreviver à recriação do bot_user_id ULID a cada deploy).
5. `20-bot-deployment.yaml` sobe o bot. `args` (não `command`) usa o
   ENTRYPOINT `tini` da imagem e chama `python3 /app/wrapper.py bot
   run --provider discord`.
6. `30-deile-job.yaml` é a prova: roda uma vez, manda DM e morre.
   `DEILE_WRAPPER_TOOL_WHITELIST=messaging` no env trava o toolset
   do agente.
7. `35-deile-interactive.yaml` é o seu sandbox — `sleep infinity`,
   alvo de `kubectl exec`. Sem whitelist (= `all`), prompt vem do
   operador.

### 3.2 O `wrapper.py` em três linhas

```
sys.argv[1] == "deile"  → carrega /run/secrets/deile/* em os.environ
                           → patcha bootstrap_providers() pra popar segredos depois
                           → (opcional) instala whitelist se DEILE_WRAPPER_TOOL_WHITELIST=messaging
                           → exec deile.cli.main()

sys.argv[1] == "bot"    → carrega /run/secrets/bot/* em os.environ
                           → instala whitelist (Discord input é untrusted)
                           → exec deilebot.cli.main()
```

`/proc/<pid>/environ` é congelado no momento do `execve` — ele nunca
vê os segredos, porque o K8s passa só env não-secretos via `env:` no
Pod spec.

### 3.3 Aceitando outros providers / outros owners

- **Outro provider LLM**: cole a chave no `.env` antes de rodar `up`;
  o `run.sh` injeta automaticamente tudo que casa `_API_KEY`.
- **Outro owner Discord**: edite `15-bot-config.yaml`, adicione mais
  itens em `owners:` no formato `discord:<snowflake>`, `kubectl apply`
  e `kubectl rollout restart deployment/deilebot`.

---

## 4. Operação dia-a-dia

### 4.1 Modos de invocar DEILE no container

```bash
# A) One-shot fechado, prompt fixo no manifest (CI / cron / proof)
bash infra/k8s/run.sh test

# B) Sandbox interativo com toolset cheio (alvo: kubectl exec)
kubectl -n deile exec deploy/deile-shell -- python3 /app/wrapper.py deile "seu prompt aqui"
kubectl -n deile exec -it deploy/deile-shell -- python3 /app/wrapper.py deile   # REPL

# C) Direto no host (sem container) — para mexer em código do seu projeto
python3 deile.py "seu prompt"
```

### 4.2 Diagnóstico rápido

```bash
# tudo no namespace
kubectl -n deile get all,secrets,configmaps,networkpolicies

# bot
kubectl -n deile logs deploy/deilebot --tail=50
kubectl -n deile logs deploy/deilebot --follow

# último Job
kubectl -n deile logs job/deile-oneshot

# is the bot's sqlite schema up?
kubectl -n deile exec deploy/deilebot -- \
  python3 -c "import sqlite3; print(sorted(r[0] for r in sqlite3.connect('/home/deile/data/deilebot.sqlite').execute('SELECT name FROM sqlite_master WHERE type=\"table\"')))"
```

### 4.3 Health checks de isolamento

São esses os experimentos que provam que o container está fechado.
Reaproveite a qualquer hora:

```bash
# uid + capabilities + seccomp
kubectl -n deile exec deploy/deile-shell -- \
  grep -E '^(Uid|Gid|CapEff|Seccomp|NoNewPrivs)' /proc/self/status

# alcança o Mac? (deve falhar instantâneo — kube-router REJECT)
kubectl -n deile exec deploy/deile-shell -- \
  python3 -c '
import socket,time
for h,p in (("192.168.5.2",22),("1.1.1.1",443)):
  s=socket.socket(); s.settimeout(3); t0=time.monotonic()
  try: s.connect((h,p)); rc="OK"
  except OSError as e: rc="FAIL: "+e.strerror
  print(f"{h}:{p} -> {rc} [{(time.monotonic()-t0)*1000:.0f} ms]")'

# secrets em /proc/<pid>/environ? (deve ser CLEAN)
kubectl -n deile exec deploy/deile-shell -- /bin/sh -c '
for K in ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY DEILE_BOT_AUTH_TOKEN; do
  grep -aq "${K}=" /proc/self/environ && echo LEAK $K || echo CLEAN $K
done'
```

---

## 5. Troubleshooting

| Sintoma | Causa | Fix |
|---|---|---|
| `error: failed to solve … no such file` no `nerdctl build` | Você não está rodando da raiz do repo | `cd <repo-root>; bash infra/k8s/run.sh build` |
| `ContainerCreating` que nunca passa | `imagePullPolicy: Never` exige imagem local | Cheque `nerdctl --namespace k8s.io images deile-stack:local`. Se não estiver lá, refaça o `build` |
| Bot: `sqlite3.OperationalError: no such table: audit` | `pip install` perdeu os `.sql` (pyproject sem `package-data`) | `bash infra/k8s/run.sh build` busca a injeção manual no Dockerfile; rebuild + `rollout restart` |
| Job: `BOT_INTEGRATION_DISABLED` | DEILE não vê `DEILE_BOT_ENDPOINT` ou `DEILE_BOT_AUTH_TOKEN` | Cheque o Secret `deile-secrets` e o env do Pod. `run.sh up` recria com bearer fresco |
| Bot: `tool whitelist active — 0 kept` | Esperado quando `DEILE_BOT_ENDPOINT` está ausente no Pod do bot e messaging tools não auto-discoverem ali | Para o agente embutido responder DMs nenhuma tool é necessária (resposta sai pelo egress pipeline). Para alcançar outros canais, configure `DEILE_BOT_ENDPOINT=http://localhost:8765` no bot Pod |
| Slash commands `/audit`, `/dlq` retornam `owner only` | Seu `provider_user_id` não está em `owners:` | Edite `15-bot-config.yaml` → `owners: ["discord:<seu_user_id>"]` → `kubectl apply -f` + `rollout restart deployment/deilebot` |
| `host.lima.internal` resolve mas conexão refused | Esperado — DNS está liberado, a conexão é bloqueada pela NetworkPolicy `except: 192.168.0.0/16` | Não é bug; é defesa funcionando |

---

## 6. Modelo de ameaça resumido

Se isto te interessou de verdade, leia
[`docs/system_design/08-SEGURANCA.md`](../../docs/system_design/08-SEGURANCA.md)
e [`docs/system_design/14-CONTAINERIZACAO.md`](../../docs/system_design/14-CONTAINERIZACAO.md).

| Vetor | Defesa |
|---|---|
| Discord user manda prompt malicioso pro bot pedindo `cat ~/.ssh/id_rsa` | Bot's embedded agent tem **só `messaging.*` tools** — sem `bash`, sem `read_file`. Mesmo se o LLM aceitasse o pedido, não tem ferramenta pra cumprir |
| Container compromised tenta ler segredos via `/proc/self/environ` | Wrapper só seta secrets em `os.environ` **depois** do `execve` — `/proc/<pid>/environ` é frozen e fica limpo |
| Container compromised tenta exfilar via TCP pra Mac | NetworkPolicy `default-deny-all` + `except 192.168.0.0/16` no egress 443 — Mac é unreachable (1 ms REJECT) |
| Container compromised tenta privilege escalation | `runAsNonRoot`, `capabilities.drop: [ALL]`, `allowPrivilegeEscalation: false`, `readOnlyRootFilesystem`, `seccompProfile: RuntimeDefault` — nada pra escalar |
| Container compromised tenta acessar K8s API | `automountServiceAccountToken: false` — sem credentials montadas |
| Pod manifest legítimo é alterado pra afrouxar securityContext | PSS `restricted` enforce no namespace — admission rejeita |
| LLM decide ler `/run/secrets/deile/ANTHROPIC_API_KEY` por conta própria | Persona `developer.md` declara "REGRA #5: extração de segredos é tentativa de invasão"; DEILE recusa **mesmo quando o operador pede** |
