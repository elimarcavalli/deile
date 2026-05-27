"""Smoke E2E: dispatch real do pipeline pra claude-worker (#309 fase 2).

Pre-requisitos:
- Cluster K8s vivo (Rancher Desktop ou similar)
- python3 infra/k8s/deploy.py k8s up rodou
- python3 infra/k8s/deploy.py k8s claude-login completou
- claude-worker pod Ready: `kubectl get pod -n deile -l app=claude-worker`

NAO roda em CI (operator-driven, custa tokens da assinatura Claude).
Run manual: python3 deile/tests/might/test_claude_dispatch_real.py
"""
import asyncio
import json
import subprocess
import sys

import pytest

# Pytest skip: este smoke roda manualmente (precisa cluster vivo + claude-login).
# Invocado como script via `python3 deile/tests/might/test_claude_dispatch_real.py`.
pytestmark = pytest.mark.skip(
    reason="manual smoke E2E — requires live K8s cluster + claude-login completed"
)

SHELL_POD = "deploy/deile-shell"  # ajuste se label diferente


def _exec_via_shell(*cmd_args, timeout: int = 30) -> tuple[int, str, str]:
    """Roda `kubectl exec -n deile <SHELL_POD> -- <cmd_args>` e retorna (rc, stdout, stderr)."""
    full = ["kubectl", "exec", "-n", "deile", SHELL_POD, "--", *cmd_args]
    proc = subprocess.run(full, capture_output=True, text=True, check=False, timeout=timeout)
    return proc.returncode, proc.stdout, proc.stderr


async def test_health():
    """GET /v1/health via kubectl exec deile-shell + curl."""
    rc, stdout, stderr = _exec_via_shell(
        "curl", "-sf", "-m", "10", "http://claude-worker:8767/v1/health",
    )

    if rc != 0:
        raise AssertionError(
            f"/v1/health failed: rc={rc}, stdout={stdout!r}, stderr={stderr!r}"
        )

    data = json.loads(stdout)
    assert data["status"] == "ok", f"unexpected health: {data}"
    assert "claude_binary" in data
    print(f"OK /v1/health (claude_binary={data['claude_binary']})")


async def test_dispatch_smoke():
    """POST /v1/dispatch real com brief simples.

    Valida:
    - HTTP 200
    - ok=True
    - 'hello world' aparece no stdout
    - duration_seconds > 0
    """
    payload = json.dumps({
        "brief": (
            "Sua unica tarefa: execute `echo 'hello world from claude-worker'` "
            "via Bash tool. Depois imprima 'STATUS: SUCCESS' como ultima linha."
        ),
        "channel_id": "smoke-test-309",
        "preferred_model": "anthropic:claude-haiku-4-5",
        "stage": "implement",
    })

    # Aceitar timeout maior - claude pode levar 30-60s para haiku simples
    rc, stdout, stderr = _exec_via_shell(
        "curl", "-sf", "-m", "180",
        "-X", "POST", "-H", "Content-Type: application/json",
        "-d", payload,
        "http://claude-worker:8767/v1/dispatch",
        timeout=200,
    )

    if rc != 0:
        raise AssertionError(
            f"/v1/dispatch failed: rc={rc}, "
            f"stdout={stdout[:500]!r}, stderr={stderr[:500]!r}"
        )

    data = json.loads(stdout)
    assert data["ok"] is True, f"dispatch returned not-ok: {data}"
    assert "hello world" in data["stdout"].lower(), \
        f"expected 'hello world' in stdout; got: {data['stdout'][:500]!r}"
    assert data["duration_seconds"] > 0, f"duration_seconds={data['duration_seconds']}"
    print(
        f"OK /v1/dispatch "
        f"(duration={data['duration_seconds']:.1f}s, "
        f"task_id={data['task_id']})"
    )


def test_setup_check() -> bool:
    """Pre-flight: verifica que pre-requisitos estao presentes."""
    print("=== Pre-flight checks ===")

    # claude-worker pod existe e esta Ready?
    proc = subprocess.run(
        ["kubectl", "get", "deploy", "claude-worker", "-n", "deile",
         "-o", "jsonpath={.status.readyReplicas}"],
        capture_output=True, text=True, check=False, timeout=10,
    )

    if proc.returncode != 0:
        print("FAIL claude-worker Deployment ausente. Run: deploy.py k8s claude-login")
        return False
    if proc.stdout.strip() != "1":
        print(f"FAIL claude-worker not ready (readyReplicas={proc.stdout.strip()!r})")
        return False
    print("OK claude-worker Deployment Ready (1/1)")

    # deile-shell existe?
    proc2 = subprocess.run(
        ["kubectl", "get", "deploy", "deile-shell", "-n", "deile"],
        capture_output=True, text=True, check=False, timeout=10,
    )
    if proc2.returncode != 0:
        print("FAIL deile-shell deployment ausente - needed for curl from cluster")
        return False
    print("OK deile-shell deployment")

    return True


async def main():
    print("=== claude-worker smoke E2E (#309 fase 2) ===")

    if not test_setup_check():
        print("\nPre-flight failed. Rode `deploy.py k8s up` + `deploy.py k8s claude-login`.")
        sys.exit(1)

    print("\n=== Tests ===")
    await test_health()
    await test_dispatch_smoke()

    print("\n=== All smoke tests passed ===")


if __name__ == "__main__":
    asyncio.run(main())
