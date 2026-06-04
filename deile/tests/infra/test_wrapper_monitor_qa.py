"""Tests for the ``monitor-qa`` role in ``infra/k8s/wrapper.py``.

The read-only Q&A path is security-critical: a prompt-injected mutation must
never reach the shell. These tests pin (1) the mutating-command regex, (2) the
bash guard's refuse/delegate decision, (3) the CLI role routing, and (4) the
hard-fail when no LLM key is present.
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
# 1. The mutating-command regex
# --------------------------------------------------------------------------- #

MUTATING = [
    "kubectl delete pod foo",
    "kubectl  patch deployment x -p '{}'",
    "kubectl apply -f m.yaml",
    "kubectl scale deploy/x --replicas=0",
    "kubectl rollout restart deploy/x",
    "kubectl set env deploy/x A=B",
    "kubectl exec deploy/x -- sh",
    "git push origin main",
    "git commit -am wip",
    "git reset --hard",
    "gh pr merge 5 --squash",
    "gh issue create --title x",
    "glab mr create",
    "helm upgrade r chart",
    "rm -rf /tmp/x",
    "mv a b",
    "cp a b",
    "chmod 777 x",
    "sudo kubectl get pods",
    "echo hi > /tmp/out",
    "kubectl get pods >> log.txt",
]

READ_ONLY = [
    "kubectl get pods",
    "kubectl get pod -l app=deile-pipeline -o json",
    "kubectl describe pod foo",
    "kubectl logs deploy/deile-pipeline --tail=50",
    "kubectl logs deploy/x 2>&1",
    "kubectl top pods",
    "gh pr list",
    "gh issue view 5",
    "gh api repos/o/r/issues",
    "glab mr list",
    "curl -s http://deile-pipeline-status:8768/v1/pipeline-status",
    "cat /state/monitor-state.json",
    "tail -20 /state/monitor-audit.log",
    "grep -c ERROR /state/monitor-audit.log",
    "ls /state",
]


@pytest.mark.parametrize("cmd", MUTATING)
def test_mutating_regex_matches(wrapper, cmd):
    assert wrapper._QA_MUTATING_RE.search(cmd), f"should be flagged: {cmd!r}"


@pytest.mark.parametrize("cmd", READ_ONLY)
def test_readonly_regex_allows(wrapper, cmd):
    assert not wrapper._QA_MUTATING_RE.search(cmd), f"should NOT be flagged: {cmd!r}"


# --------------------------------------------------------------------------- #
# 2. The bash guard (regex + assess_risk) — refuse vs delegate
# --------------------------------------------------------------------------- #


class _FakeContext:
    def __init__(self, command):
        self.parsed_args = {"command": command}


class _FakeBashTool:
    """Minimal stand-in exposing ``execute_sync`` like BashExecuteTool."""

    def __init__(self):
        self.calls = []

    def execute_sync(self, context):
        self.calls.append(context.parsed_args.get("command"))
        return "DELEGATED"


def test_guard_refuses_mutations(wrapper):
    tool = _FakeBashTool()
    wrapper._wrap_bash_readonly(tool)
    from deile.tools.base import ToolResult

    for cmd in ["kubectl delete pod foo", "git push origin main",
                "rm -rf /", "echo x > f", "gh pr merge 5"]:
        result = tool.execute_sync(_FakeContext(cmd))
        assert isinstance(result, ToolResult), cmd
        assert result.is_success is False, cmd
        assert "recusado" in (result.message or "").lower(), cmd
    assert tool.calls == [], "refused commands must not reach the real shell"


def test_guard_delegates_reads(wrapper):
    tool = _FakeBashTool()
    wrapper._wrap_bash_readonly(tool)
    for cmd in ["kubectl get pods", "gh pr list",
                "curl -s http://x:8768/v1/pipeline-status",
                "cat /state/monitor-state.json"]:
        result = tool.execute_sync(_FakeContext(cmd))
        assert result == "DELEGATED", f"read command should delegate: {cmd!r}"
    assert len(tool.calls) == 4


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
