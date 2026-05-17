#!/usr/bin/env bash
# Shim de compatibilidade — o orquestrador agora é o deploy.py (Python).
#
# Toda a lógica vive em infra/k8s/deploy.py. Este script existe só para
# que `bash infra/k8s/run.sh <comando>` continue funcionando (muscle
# memory, docs antigas). Use o deploy.py diretamente daqui em diante:
#
#   python3 infra/k8s/deploy.py help
#
exec python3 "$(dirname "$0")/deploy.py" "$@"
