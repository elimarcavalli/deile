#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO_ROOT=$(cd "$SCRIPT_DIR/.." && pwd)
DEFAULT_CONFIG_FILE="$REPO_ROOT/infra/k8s/manifests/47-deile-runtime-config.yaml"
DEFAULT_NAMESPACE="deile"
DEFAULT_HPA_NAME="deile-worker"

usage() {
  cat <<USAGE
Usage: $(basename "$0") [options]

Options:
  --config-file PATH   Path to the runtime config manifest (default: $DEFAULT_CONFIG_FILE)
  --namespace NS       Kubernetes namespace where the HPA lives (default: $DEFAULT_NAMESPACE)
  --hpa-name NAME      HorizontalPodAutoscaler resource name (default: $DEFAULT_HPA_NAME)
  --kubectl PATH       Path to the kubectl binary (env var KUBECTL_BIN is respected too)
  --dry-run           Run kubectl patch with --dry-run=client
  --help              Show this help message

The script reads worker.hpa.* keys from the ConfigMap manifest and patches the
existing HPA so operators do not have to edit the resource manually.
USAGE
}

config_file="$DEFAULT_CONFIG_FILE"
namespace="$DEFAULT_NAMESPACE"
hpa_name="$DEFAULT_HPA_NAME"
kubectl_bin="${KUBECTL_BIN:-kubectl}"
dry_run=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    --config-file)
      config_file="$2"
      shift 2
      ;;
    --namespace)
      namespace="$2"
      shift 2
      ;;
    --hpa-name)
      hpa_name="$2"
      shift 2
      ;;
    --kubectl)
      kubectl_bin="$2"
      shift 2
      ;;
    --dry-run)
      dry_run=true
      shift
      ;;
    --help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown argument: $1" >&2
      usage
      exit 1
      ;;
  esac
done

if [[ ! -f "$config_file" ]]; then
  echo "Config file not found: $config_file" >&2
  exit 1
fi

if ! command -v "$kubectl_bin" >/dev/null 2>&1; then
  echo "kubectl binary not found at $kubectl_bin" >&2
  exit 1
fi

read -r min_replicas max_replicas target_average <<<"$({
  python3 - "$config_file" <<'PY'
import sys
import pathlib
import yaml

path = pathlib.Path(sys.argv[1])
if not path.exists():
    raise SystemExit(f"config file {path} not found")
raw = yaml.safe_load(path.read_text())
if not isinstance(raw, dict):
    raise SystemExit("config file must contain a mapping at the top level")
data = raw.get("data", {})
keys = [
    ("worker.hpa.minReplicas", "minReplicas"),
    ("worker.hpa.maxReplicas", "maxReplicas"),
    ("worker.hpa.targetAverageValue", "targetAverageValue"),
]
values = {}
missing = []
for key, label in keys:
    value = data.get(key)
    if value is None or str(value).strip() == "":
        missing.append(key)
    else:
        values[label] = str(value).strip()
if missing:
    raise SystemExit(f"missing runtime config keys: {', '.join(missing)}")
print(values["minReplicas"])
print(values["maxReplicas"])
print(values["targetAverageValue"])
PY
})"

if [[ -z "$min_replicas" || -z "$max_replicas" || -z "$target_average" ]]; then
  echo "Failed to parse runtime config" >&2
  exit 1
fi

if ! [[ "$min_replicas" =~ ^[0-9]+$ ]] || ! [[ "$max_replicas" =~ ^[0-9]+$ ]]; then
  echo "minReplicas and maxReplicas must be integers" >&2
  exit 1
fi

min_value=$((min_replicas))
max_value=$((max_replicas))

if (( min_value > max_value )); then
  echo "minReplicas ($min_value) must not exceed maxReplicas ($max_value)" >&2
  exit 1
fi

if (( max_value < 2 )); then
  echo "maxReplicas ($max_value) must be at least 2 for safe parallelism" >&2
  exit 1
fi

if [[ -z "$target_average" ]]; then
  echo "targetAverageValue must not be empty" >&2
  exit 1
fi

patch_payload=$(cat <<PATCH
spec:
  minReplicas: $min_value
  maxReplicas: $max_value
  metrics:
    - type: Resource
      resource:
        name: cpu
        target:
          type: AverageValue
          averageValue: "$target_average"
PATCH
)

echo "Patching HPA $hpa_name in namespace $namespace (min=$min_value, max=$max_value, target=$target_average)"

cmd=("$kubectl_bin" patch hpa "$hpa_name" --namespace "$namespace" --type merge --patch "$patch_payload")
if [[ "$dry_run" == true ]]; then
  cmd+=(--dry-run=client)
fi

"${cmd[@]}"
