"""Tests for the ``monitor-qa`` role in ``infra/k8s/wrapper.py``.

The read-only Q&A path is security-critical: a prompt-injected mutation must
never reach the shell. The defense is an ALLOW-LIST executor (shell-free), so
these tests pin (1) ``_qa_command_allowed`` against every bypass vector the
review surfaced, (2) that the executor is genuinely shell-free (chaining /
redirection are inert), (3) CLI role routing, and (4) the no-LLM-key hard-fail.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_INFRA = str(_REPO / "infra" / "k8s")
if _INFRA not in sys.path:
    sys.path.insert(0, _INFRA)


@pytest.fixture
def wrapper():
    import wrapper as _w

    return _w


# --------------------------------------------------------------------------- #
# 1. _qa_command_allowed — the allow-list decision (security crux)
# --------------------------------------------------------------------------- #

# Every one of these is a real bypass vector from the adversarial review; ALL
# must be REFUSED by the allow-list.
REJECTED = [
    # unlisted binaries — the apiserver/curl + interpreter bypasses
    'curl -sk -X DELETE -H "Authorization: Bearer x" https://10.43.0.1/api/v1/namespaces/deile/pods/p',
    "curl -s http://x/v1/status",
    'python3 -c "import os; os.remove(1)"',
    "python -c 'x'",
    "perl -e '1'",
    "node -e '1'",
    "find / -delete",
    "find . -name x -exec rm {} +",
    "sed -i s/a/b/ f",
    "awk 'BEGIN{system(\"rm x\")}'",
    "helm uninstall r",
    "bash -c 'rm -rf /'",
    "sh -c x",
    "eval x",
    "xargs rm",
    # kubectl write/exec verbs
    "kubectl delete pod foo",
    "kubectl run evil --image=alpine",
    "kubectl debug node/n1 -it --image=alpine",
    "kubectl exec deploy/x -- sh",
    "kubectl apply -f m.yaml",
    "kubectl patch deploy x -p {}",
    "kubectl scale deploy/x --replicas=0",
    "kubectl edit deploy x",
    "kubectl proxy --port=8001",
    # kubectl secret reads + raw
    "kubectl get secret claude-credentials -o yaml",
    "kubectl get secrets",
    "kubectl describe secret/claude-credentials",
    "kubectl get --raw /api/v1/namespaces/deile/pods",
    # variable indirection / reassembly (shlex makes argv0 not an allowed binary)
    "k=kubectl; $k delete pod x",
    # gh/glab writes + api writes (separate-token forms)
    "gh pr merge 5 --squash",
    "gh issue create --title x",
    "glab mr create",
    "gh api -X POST repos/o/r/issues -f title=x",
    "gh api --method DELETE repos/o/r/issues/1",
    "gh api repos/o/r/issues -f body=x",
    # gh/glab api writes — ATTACHED / EQUALS flag forms (the re-review bypass)
    "gh api -XDELETE repos/o/r/issues/comments/1",
    "gh api -XPOST repos/o/r/issues",
    "gh api --method=DELETE repos/o/r/git/refs/heads/main",
    "gh api -ftitle=pwn repos/o/r/issues",
    "gh api -Fbody=x repos/o/r/issues",
    "gh api --field=title=x repos/o/r/issues",
    "glab api -XDELETE projects/1/repository/branches/main",
    # token disclosure
    "gh auth status --show-token",
    "gh auth token",
    "glab auth status -t",
    "glab config get -h gitlab.com token",
    "gh config get oauth_token",
    # kubectl raw-API / impersonation / endpoint override (equals + space forms)
    "kubectl get --raw=/api/v1/namespaces/deile/secrets/claude-credentials",
    "kubectl get --raw /api/v1/namespaces/deile/secrets/claude-credentials",
    "kubectl get pods --as=system:admin",
    "kubectl get pods --server=https://evil",
    "kubectl get pods --token=abc",
    # kubectl config write-subcommands (config is a read verb but mutates here)
    "kubectl config set-context monitor --namespace=kube-system",
    "kubectl config set clusters.in-cluster.server https://evil",
    "kubectl config delete-context monitor",
    "kubectl config unset users",
    "kubectl config use-context other",
    # write-capable coreutils dropped from the allow-list
    "sort -o /tmp/out /etc/hosts",
    "sort --output=/tmp/out /etc/hosts",
    "uniq /etc/hosts /tmp/out",
    # secret-path reads
    "cat /run/secrets/monitor/MONITOR_BEARER_TOKEN",
    "cat /run/secrets/deile/GITHUB_TOKEN",
    "cat /var/run/secrets/kubernetes.io/serviceaccount/token",
    "cat /proc/self/environ",
    "tail /home/deile/.git-credentials",
    "",
    "   ",
]

ALLOWED = [
    "kubectl get pods",
    "kubectl get pod -l app=deile-pipeline -o json",
    "kubectl describe pod foo",
    "kubectl logs deploy/deile-pipeline --tail=50",
    "kubectl top pods",
    "kubectl get events",
    "kubectl version",
    "gh pr list",
    "gh issue view 5",
    "gh api repos/o/r/issues",
    "gh api -X GET repos/o/r",
    "gh api -XGET repos/o/r/pulls",
    "gh auth status",
    "kubectl get pods -o yaml",
    "kubectl logs deploy/deile-pipeline --tail=50 --since=10m",
    "kubectl config view",
    "kubectl config current-context",
    "glab mr list",
    "cat /state/monitor-state.json",
    "tail -20 /state/monitor-audit.log",
    "grep ERROR /state/monitor-audit.log",
    "jq . /state/monitor-state.json",
    "ls /state",
]


@pytest.mark.parametrize("cmd", REJECTED)
def test_qa_command_rejected(wrapper, cmd):
    ok, reason = wrapper._qa_command_allowed(cmd)
    assert ok is False, f"MUST be refused: {cmd!r}"
    assert reason, "a refusal must carry a reason"


@pytest.mark.parametrize("cmd", ALLOWED)
def test_qa_command_allowed(wrapper, cmd):
    ok, reason = wrapper._qa_command_allowed(cmd)
    assert ok is True, f"read command should be allowed: {cmd!r} (reason={reason!r})"


# --------------------------------------------------------------------------- #
# 2. The executor is genuinely shell-free (chaining / redirection are inert)
# --------------------------------------------------------------------------- #


class _Ctx:
    def __init__(self, command):
        self.parsed_args = {"command": command}


class _Bash:
    """Stand-in exposing execute_sync; the wrap REPLACES it."""

    def execute_sync(self, context):  # pragma: no cover - replaced by the wrap
        raise AssertionError("original execute_sync must never be called (shell path)")


def test_executor_refuses_disallowed_without_running(wrapper):
    tool = _Bash()
    wrapper._wrap_bash_readonly(tool)
    from deile.tools.base import ToolResult

    res = tool.execute_sync(_Ctx("kubectl delete pod foo"))
    assert isinstance(res, ToolResult) and res.is_success is False
    assert "recusado" in (res.message or "").lower()


def test_executor_runs_allowed_shell_free(wrapper, tmp_path):
    tool = _Bash()
    wrapper._wrap_bash_readonly(tool)
    # `echo` is allow-listed; a `;` + `rm` chain must be INERT (literal args to
    # echo), proving there is no shell. The sentinel file must survive.
    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    res = tool.execute_sync(_Ctx(f"echo hi ; rm -rf {victim}"))
    assert res.is_success is True
    assert "hi" in (res.data or "")
    assert victim.exists(), "shell-free: the `; rm` chain must NOT have executed"


def test_executor_redirection_is_inert(wrapper, tmp_path):
    tool = _Bash()
    wrapper._wrap_bash_readonly(tool)
    target = tmp_path / "should_not_exist.txt"
    res = tool.execute_sync(_Ctx(f"echo pwned > {target}"))
    # echo prints the literal "pwned > <path>"; no file is written by a shell.
    assert res.is_success is True
    assert not target.exists(), "shell-free: `>` redirection must NOT create a file"


# --------------------------------------------------------------------------- #
# 3. CLI role routing
# --------------------------------------------------------------------------- #


def test_main_routes_monitor_qa(wrapper, monkeypatch):
    seen = {}

    def _fake(rest):
        seen["rest"] = rest
        return 0

    monkeypatch.setattr(wrapper, "_run_monitor_qa", _fake)
    rc = wrapper.main(["wrapper.py", "monitor-qa", "como tá o cluster?"])
    assert rc == 0
    assert seen["rest"] == ["como tá o cluster?"]


def test_main_unknown_role(wrapper):
    assert wrapper.main(["wrapper.py", "bogus"]) == 64


def test_main_usage_when_no_role(wrapper):
    assert wrapper.main(["wrapper.py"]) == 64


# --------------------------------------------------------------------------- #
# 4. Hard-fail without an LLM key
# --------------------------------------------------------------------------- #


def test_monitor_qa_no_llm_key_returns_78(wrapper, monkeypatch):
    monkeypatch.setattr(wrapper, "_harden_runtime_dirs", lambda: None)
    monkeypatch.setattr(wrapper, "_load_secret_files", lambda role_dir: [])
    rc = wrapper._run_monitor_qa(["pergunta"])
    assert rc == 78
