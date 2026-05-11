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
  local api_key_args=()
  for key in ANTHROPIC_API_KEY OPENAI_API_KEY DEEPSEEK_API_KEY GOOGLE_API_KEY; do
    local v
    v="$(read_env_var "$key" "$ENV_FILE" || true)"
    if [ -n "$v" ]; then
      api_key_args+=( "--from-literal=${key}=${v}" )
    fi
  done
  [ "${#api_key_args[@]}" -gt 0 ] || fail "no LLM API key in $ENV_FILE"

  log "wiring Secrets (atomic apply; nothing echoed)"
  # `create … --dry-run=client -o yaml | apply` avoids the delete+create
  # race where a pod restarts after the delete but before the create
  # and finds no secret to mount.
  #
  # Bot side: Discord token + control-plane Bearer + LLM key (the
  # embedded agent needs a key to reply). The tool whitelist in
  # wrapper.py blocks bash/file/exec from any Discord-driven prompt.
  "$KUBECTL" -n "$NS" create secret generic bot-secrets \
    --from-literal=DEILE_BOT_DISCORD_TOKEN="$discord_token" \
    --from-literal=DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN="$bearer_token" \
    "${api_key_args[@]}" \
    --dry-run=client -o yaml | "$KUBECTL" apply -f - >/dev/null

  # Deile side: same LLM key + Bearer to talk to bot.
  "$KUBECTL" -n "$NS" create secret generic deile-secrets \
    "${api_key_args[@]}" \
    --from-literal=DEILE_BOT_AUTH_TOKEN="$bearer_token" \
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
  down)    cmd_down ;;
  all)     cmd_all ;;
  *)
    cat >&2 <<EOF
usage: $0 {build|up|test|logs|down|all}

  build  — rebuild the deile-stack:local image into containerd's k8s.io ns
           (auto-rolls existing deployments so they pick up the new image)
  up     — namespace, network policies, secrets, bot deployment+svc (idempotent)
  test   — run the one-shot deile Job; streams logs
  logs   — show recent bot + Job logs
  down   — delete the deile namespace (full teardown)
  all    — build, up, test, logs (the happy path)
EOF
    exit 64
    ;;
esac
