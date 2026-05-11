#!/usr/bin/env bash
# orchestration — build the image, prepare Secrets, deploy bot, run
# the deile one-shot Job, follow logs, then tear down on demand.
#
# This script never prints secret material. It reads secrets from the
# host's local `.env` file (same one DEILE/deilebot use today) and
# pipes them into `kubectl create secret` via stdin.
#
# Usage:
#   bash infra/k8s/run.sh build      # rebuild the image (slow first time)
#   bash infra/k8s/run.sh up         # apply namespace, secrets, bot, NPs
#   bash infra/k8s/run.sh test       # run the one-shot deile Job
#   bash infra/k8s/run.sh logs       # tail bot+job logs
#   bash infra/k8s/run.sh down       # delete the namespace (everything)
#   bash infra/k8s/run.sh all        # build + up + test + logs

set -euo pipefail

NS="deile"
IMAGE="deile-stack:local"
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
ENV_FILE="$ROOT/.env"

# Fall back to PATH lookup; if not found, try the Rancher Desktop location.
NERDCTL="${NERDCTL:-$(command -v nerdctl 2>/dev/null || echo /Users/elimar.cavalli/.rd/bin/nerdctl)}"
KUBECTL="${KUBECTL:-$(command -v kubectl 2>/dev/null || echo /Users/elimar.cavalli/.rd/bin/kubectl)}"

log() { printf '\033[1;36m[run.sh]\033[0m %s\n' "$*" >&2; }
fail() { printf '\033[1;31m[run.sh]\033[0m %s\n' "$*" >&2; exit 1; }

require() {
  for cmd in "$@"; do
    command -v "$cmd" >/dev/null 2>&1 || fail "missing required tool: $cmd"
  done
}

require_file() {
  [ -f "$1" ] || fail "expected file not found: $1"
}

read_env_var() {
  # Resolve VAR=VALUE from the .env. Defers to python-dotenv when
  # available so we honor the same quoting/escape rules deile itself
  # uses (deile/cli.py:_load_dotenv); falls back to a minimal POSIX
  # parser if dotenv is missing in the operator's environment.
  local var="$1" file="$2"
  if python3 -c "import dotenv" >/dev/null 2>&1; then
    python3 - "$var" "$file" <<'PY'
import sys
from dotenv import dotenv_values
key, path = sys.argv[1], sys.argv[2]
print(dotenv_values(path).get(key) or "")
PY
  else
    awk -v key="$var" '
      BEGIN { FS = "=" }
      /^[[:space:]]*#/ { next }
      $1 == key {
        sub(/^[^=]*=/, "")
        gsub(/^[ \t"'\'']+|[ \t"'\'']+$/, "")
        print
        exit
      }
    ' "$file"
  fi
}

cmd_build() {
  log "building image $IMAGE in containerd namespace k8s.io"
  require_file "$ROOT/pyproject.toml"
  require_file "$ROOT/deilebot/pyproject.toml"
  # `--namespace k8s.io` is what k3s reads from; without it the image
  # builds into nerdctl's default namespace and k8s never sees it.
  "$NERDCTL" --namespace k8s.io build \
    -f "$HERE/Dockerfile" \
    -t "$IMAGE" \
    "$ROOT"
  log "image built — verifying it lives in the k3s namespace"
  "$NERDCTL" --namespace k8s.io images "$IMAGE" | tail -1

  # With imagePullPolicy: Never, K8s caches images by tag. A rebuild
  # leaves the tag identical but the digest changed — running pods
  # keep serving the OLD image until restarted. Force a rollout
  # whenever the bot/shell are already deployed.
  for dep in deilebot deile-shell; do
    if "$KUBECTL" -n "$NS" get deployment "$dep" >/dev/null 2>&1; then
      log "rolling deployment/$dep so it picks up the new image"
      "$KUBECTL" -n "$NS" rollout restart "deployment/$dep" >/dev/null
    fi
  done
}

cmd_up() {
  require_file "$ENV_FILE"
  local discord_token bearer_token
  discord_token="$(read_env_var DEILE_BOT_DISCORD_TOKEN "$ENV_FILE")"
  [ -n "$discord_token" ] || fail "DEILE_BOT_DISCORD_TOKEN missing in $ENV_FILE"

  # Try to reuse a stable bearer token from the env; if absent, mint a
  # fresh 32-byte URL-safe one. Either way it stays in the cluster
  # Secrets only — never printed.
  bearer_token="$(read_env_var DEILE_BOT_AUTH_TOKEN "$ENV_FILE" || true)"
  if [ -z "$bearer_token" ]; then
    bearer_token="$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
    log "minted fresh bearer token (32 bytes); existing pods will be reset"
  fi

  log "applying namespace + network policies"
  "$KUBECTL" apply -f "$HERE/manifests/00-namespace.yaml"
  "$KUBECTL" apply -f "$HERE/manifests/40-network-policy.yaml"

  # Collect LLM keys that exist in .env. The bot needs at least one
  # for its embedded agent to reply to Discord; deile needs at least
  # one for its own outbound flow.
  local api_key_pairs=""
  local _api_key_count=0
  for key in ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY; do
    local v
    v="$(read_env_var "$key" "$ENV_FILE" || true)"
    if [ -n "$v" ]; then
      api_key_pairs+="${key}=${v}"$'\n'
      _api_key_count=$(( _api_key_count + 1 ))
    fi
  done
  [ "$_api_key_count" -gt 0 ] || fail "no LLM API key in $ENV_FILE"

  log "wiring Secrets (atomic apply; nothing echoed)"
  # `create … --dry-run=client -o yaml | apply` avoids the delete+create
  # race where a pod restarts after the delete but before the create
  # and finds no secret to mount.
  #
  # Bot side: Discord token + control-plane Bearer + LLM key (the
  # embedded agent needs a key to reply). The tool whitelist in
  # wrapper.py blocks bash/file/exec from any Discord-driven prompt.
  printf "DEILE_BOT_DISCORD_TOKEN=%s\nDEILE_BOT_CONTROL_PLANE_AUTH_TOKEN=%s\n%s" \
      "$discord_token" "$bearer_token" "$api_key_pairs" | \
      "$KUBECTL" -n "$NS" create secret generic bot-secrets \
        --from-env-file=- \
        --dry-run=client -o yaml | "$KUBECTL" apply -f - >/dev/null

  # Deile side: same LLM key + Bearer to talk to bot.
  printf "DEILE_BOT_AUTH_TOKEN=%s\n%s" "$bearer_token" "$api_key_pairs" | \
      "$KUBECTL" -n "$NS" create secret generic deile-secrets \
        --from-env-file=- \
        --dry-run=client -o yaml | "$KUBECTL" apply -f - >/dev/null

  log "applying bot ConfigMap + Deployment + Service + interactive shell"
  "$KUBECTL" apply -f "$HERE/manifests/15-bot-config.yaml"
  "$KUBECTL" apply -f "$HERE/manifests/20-bot-deployment.yaml"
  "$KUBECTL" apply -f "$HERE/manifests/35-deile-interactive.yaml"

  # Env-var values from Secrets are baked into the pod's env block
  # at container creation; a Secret update alone does NOT restart
  # the pod. Force a rollout so the bot picks up whatever bearer
  # token we just minted.
  if "$KUBECTL" -n "$NS" get deployment deilebot >/dev/null 2>&1; then
    log "rolling deilebot so the new Secret values take effect"
    "$KUBECTL" -n "$NS" rollout restart deployment/deilebot >/dev/null
  fi

  log "waiting for bot to become Ready (max 120s)"
  if ! "$KUBECTL" -n "$NS" rollout status deployment/deilebot --timeout=120s; then
    "$KUBECTL" -n "$NS" describe deploy/deilebot >&2
    "$KUBECTL" -n "$NS" logs deploy/deilebot --tail=80 >&2 || true
    fail "bot did not become Ready"
  fi
}

cmd_test() {
  log "deleting any prior Job and re-applying the one-shot"
  "$KUBECTL" -n "$NS" delete job deile-oneshot --ignore-not-found >/dev/null
  "$KUBECTL" apply -f "$HERE/manifests/30-deile-job.yaml"

  log "waiting for the Job pod to start"
  # The Job controller may take a tick to spawn the pod; let kubectl
  # block on the controller's own status instead of polling by hand.
  if ! "$KUBECTL" -n "$NS" wait --for=jsonpath='{.status.active}'=1 \
        job/deile-oneshot --timeout=30s >/dev/null 2>&1; then
    # job may have already completed in <30s, that's fine
    :
  fi
  pod="$("$KUBECTL" -n "$NS" get pods -l job-name=deile-oneshot \
          -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  [ -n "${pod:-}" ] || fail "Job pod never appeared"

  log "streaming Job logs (Ctrl-C stops the stream, not the Job)"
  # `--pod-running-timeout=120s` waits for the pod, then follows logs.
  "$KUBECTL" -n "$NS" logs --pod-running-timeout=120s -f "$pod" || true

  log "Job final status:"
  "$KUBECTL" -n "$NS" get job/deile-oneshot -o wide
  "$KUBECTL" -n "$NS" get pods -l job-name=deile-oneshot
}

cmd_logs() {
  log "bot logs (tail 80):"
  "$KUBECTL" -n "$NS" logs deploy/deilebot --tail=80 || true
  echo
  log "deile job logs (tail 80):"
  pod="$("$KUBECTL" -n "$NS" get pods -l job-name=deile-oneshot \
          -o jsonpath='{.items[0].metadata.name}' 2>/dev/null || true)"
  if [ -n "$pod" ]; then
    "$KUBECTL" -n "$NS" logs "$pod" --tail=80 || true
  else
    log "no deile-oneshot pod yet"
  fi
}

cmd_clone() {
  # Clone a GitHub repo into /home/deile/work/<name> inside deile-shell.
  #
  # Requires:
  #   - GITHUB_TOKEN in .env (fine-scoped read / clone token — no write needed)
  #   - Namespace and deile-shell deployment already running (run `up` first)
  #
  # The token is wired into the deile-secrets Secret, propagated to the pod
  # by the kubelet volume sync, and picked up by wrapper.py's credential
  # helper. The ~/bin/git guard then enforces the clonable_repos allowlist
  # from bot-config before any clone proceeds.
  local repo="${1:-}"
  [ -n "$repo" ] || fail "usage: $0 clone <owner/repo>"

  require_file "$ENV_FILE"

  # Verify the namespace is up.
  "$KUBECTL" get namespace "$NS" >/dev/null 2>&1 \
    || fail "Namespace $NS not found. Run: $0 up"

  # Verify deile-shell deployment exists.
  "$KUBECTL" -n "$NS" get deployment deile-shell >/dev/null 2>&1 \
    || fail "deile-shell not running. Run: $0 up"

  local github_token
  github_token="$(read_env_var GITHUB_TOKEN "$ENV_FILE" || true)"
  [ -n "$github_token" ] || fail "GITHUB_TOKEN missing in $ENV_FILE (add a fine-scoped read/clone token)"

  # Collect LLM keys that exist in .env (needed to not wipe them from the secret).
  local api_key_pairs=""
  local _api_key_count=0
  for key in ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY; do
    local v
    v="$(read_env_var "$key" "$ENV_FILE" || true)"
    [ -n "$v" ] && { api_key_pairs+="${key}=${v}"$'\n'; _api_key_count=$(( _api_key_count + 1 )); }
  done
  [ "$_api_key_count" -gt 0 ] || fail "no LLM API key in $ENV_FILE"

  local bearer_token
  bearer_token="$(read_env_var DEILE_BOT_AUTH_TOKEN "$ENV_FILE" || true)"
  [ -n "$bearer_token" ] || fail "DEILE_BOT_AUTH_TOKEN missing in $ENV_FILE — run 'up' first to establish a stable token"

  log "wiring GITHUB_TOKEN into deile-secrets (no other secret values echoed)"
  printf "DEILE_BOT_AUTH_TOKEN=%s\nGITHUB_TOKEN=%s\n%s" \
      "$bearer_token" "$github_token" "$api_key_pairs" | \
      "$KUBECTL" -n "$NS" create secret generic deile-secrets \
        --from-env-file=- \
        --dry-run=client -o yaml | "$KUBECTL" apply -f - >/dev/null

  # kubelet syncs projected secret volumes asynchronously (~30-60 s by default).
  # Poll up to 90 s using wall-clock time so kubectl exec latency doesn't
  # cause early exit.
  log "waiting for kubelet to sync GITHUB_TOKEN into the pod (max 90s)"
  local START_TS
  START_TS=$(date +%s)
  local token_ready=0
  while [ $(( $(date +%s) - START_TS )) -lt 90 ]; do
    if "$KUBECTL" -n "$NS" exec deploy/deile-shell -- \
        test -f /run/secrets/deile/GITHUB_TOKEN 2>/dev/null; then
      log "GITHUB_TOKEN secret file confirmed in pod"
      token_ready=1
      break
    fi
    sleep 10
  done
  if [ "$token_ready" -eq 0 ]; then
    fail "GITHUB_TOKEN not synced after 90s. Diagnose: kubectl -n $NS describe secret deile-secrets && kubectl -n $NS get events"
  fi

  local repo_name="${repo##*/}"
  local work_dir="/home/deile/work/${repo_name}"
  local clone_url="https://github.com/${repo}.git"

  log "cloning ${clone_url} → ${work_dir} (inside deile-shell)"
  # Run a self-contained Python snippet inside the pod that:
  #   1. Sets up git credentials from the mounted secret file.
  #   2. Invokes git clone via the ~/bin/git guard (enforces allowlist), or
  #      performs its own URL validation if the guard is absent.
  # The token never appears in argv; it is read from the Secret-mounted
  # file and written to ~/.git-credentials inside the pod in-process.
  # CLONE_URL and WORK_DIR are passed via --env so they are never
  # interpolated into the Python source (no shell-injection risk).
  "$KUBECTL" -n "$NS" exec \
    --env CLONE_URL="${clone_url}" \
    --env WORK_DIR="${work_dir}" \
    deploy/deile-shell -- \
    python3 -c '
import fnmatch, os, posixpath, subprocess, sys, urllib.parse
from pathlib import Path

clone_url = os.environ["CLONE_URL"]
work_dir  = os.environ["WORK_DIR"]

home = Path(os.environ.get("HOME", "/home/deile"))
token_file = Path("/run/secrets/deile/GITHUB_TOKEN")
if token_file.exists():
    token = token_file.read_text().strip()
    creds = home / ".git-credentials"
    fd = os.open(str(creds), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        fh = os.fdopen(fd, "w", encoding="utf-8")
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        raise
    with fh:
        fh.write("https://oauth2:" + token + "@github.com\n")
    subprocess.run(
        ["git", "config", "--global", "credential.helper", "store"],
        check=False,
    )

(home / "work").mkdir(parents=True, exist_ok=True)
git_bin = home / "bin" / "git"

if git_bin.exists():
    # Guard present — enforces allowlist and re-injects credential.helper itself.
    git_cmd = str(git_bin)
    _cred_args = []
else:
    # Guard absent — perform URL validation using the config file when present.
    # If both guard and config are absent, open policy applies (any repo allowed),
    # matching the documented wrapper.py behaviour for absent config.
    config_path = home / "config" / "deilebot.yaml"
    if config_path.exists():
        try:
            import yaml
        except ImportError as exc:
            print(f"clone: PyYAML not available: {exc} — deny all", file=sys.stderr)
            sys.exit(1)
        try:
            data = yaml.safe_load(config_path.read_text()) or {}
            raw = (data.get("git_integration") or {}).get("clonable_repos", [])
            if isinstance(raw, list) and raw:
                patterns = [str(p).strip() for p in raw if str(p).strip()]
            else:
                patterns = ["*"]
        except Exception as exc:
            print(f"clone: could not parse clonable_repos: {exc} — deny all", file=sys.stderr)
            sys.exit(1)
    else:
        patterns = ["*"]

    if patterns != ["*"]:
        parsed = urllib.parse.urlparse(clone_url)
        if parsed.hostname == "github.com":
            repo_path = posixpath.normpath(parsed.path.lstrip("/").removesuffix(".git"))
        else:
            repo_path = clone_url.removesuffix(".git")
        if not any(fnmatch.fnmatch(repo_path, p) for p in patterns):
            print(
                f"clone: {clone_url!r} is not in clonable_repos allowlist. "
                f"Allowed patterns: {patterns}",
                file=sys.stderr,
            )
            sys.exit(1)

    git_cmd = "/usr/bin/git"
    _cred_args = ["-c", "credential.helper=store"]

result = subprocess.run(
    [git_cmd, *_cred_args, "clone", "--depth", "1", clone_url, work_dir],
    env={**os.environ, "GIT_TERMINAL_PROMPT": "0",
         "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1"},
)
if result.returncode != 0:
    sys.exit(result.returncode)
print("clone complete: " + work_dir)
'
  log "done — repo available at ${work_dir} inside deile-shell"
  log "to work: kubectl -n ${NS} exec -it deploy/deile-shell -- python3 /app/wrapper.py deile"
}

cmd_down() {
  log "removing namespace $NS (this deletes everything in it)"
  "$KUBECTL" delete namespace "$NS" --ignore-not-found
}

cmd_all() {
  cmd_build
  cmd_up
  cmd_test
  cmd_logs
}

case "${1:-}" in
  build)   cmd_build ;;
  up)      cmd_up ;;
  test)    cmd_test ;;
  logs)    cmd_logs ;;
  clone)   cmd_clone "${2:-}" ;;
  down)    cmd_down ;;
  all)     cmd_all ;;
  *)
    cat >&2 <<EOF
usage: $0 {build|up|test|logs|clone|down|all}

  build        — rebuild the deile-stack:local image into containerd's k8s.io ns
                 (auto-rolls existing deployments so they pick up the new image)
  up           — namespace, network policies, secrets, bot deployment+svc (idempotent)
  test         — run the one-shot deile Job; streams logs
  logs         — show recent bot + Job logs
  clone <o/r>  — clone github.com/<owner>/<repo> into /home/deile/work/<repo>
                 inside deile-shell (requires GITHUB_TOKEN in .env and 'up' first)
  down         — delete the deile namespace (full teardown)
  all          — build, up, test, logs (the happy path)
EOF
    exit 64
    ;;
esac
