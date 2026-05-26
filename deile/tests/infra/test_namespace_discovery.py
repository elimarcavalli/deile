"""Testes unitários para `discover_deile_namespaces` e `_read_forge_kind`.

Mocka subprocess.run — nenhum teste toca o cluster real. Cobre os casos
de label encontrada, fallback por pod e kubectl ausente.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel_data as pd  # noqa: E402

# ===== discover_deile_namespaces ============================================

def _mock_run(responses: dict):
    """Fábrica de mock para subprocess.run.

    ``responses`` mapeia tuplas de argv para ``(returncode, stdout)`` ou
    para uma exceção.
    """
    def _run(cmd, **kw):
        for args_pattern, result in responses.items():
            if all(p in cmd for p in args_pattern):
                if isinstance(result, Exception):
                    raise result
                rc, stdout = result
                m = MagicMock()
                m.returncode = rc
                m.stdout = stdout
                return m
        # Fallback: comando não reconhecido → returncode 1, stdout vazio.
        m = MagicMock()
        m.returncode = 1
        m.stdout = ""
        return m
    return _run


class TestDiscoverDeileNamespaces:
    def test_returns_empty_when_kubectl_absent(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: None)
        assert pd.discover_deile_namespaces() == []

    def test_returns_labeled_namespaces(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns", "-l"): (0, "deile deile-gl"),
            ("get", "pods", "--all-namespaces"): (0, ""),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert result == ["deile", "deile-gl"]

    def test_returns_pod_based_namespaces_when_no_label(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns", "-l"): (0, ""),
            ("get", "pods", "--all-namespaces"): (0, "deile-github deile-gitlab"),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert result == ["deile-github", "deile-gitlab"]

    def test_merges_label_and_pod_sources(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns", "-l"): (0, "deile-a"),
            ("get", "pods", "--all-namespaces"): (0, "deile-b"),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert sorted(result) == ["deile-a", "deile-b"]

    def test_deduplicates_namespaces(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns", "-l"): (0, "deile"),
            ("get", "pods", "--all-namespaces"): (0, "deile"),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert result == ["deile"]

    def test_handles_kubectl_timeout_gracefully(self, monkeypatch):
        import subprocess
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns"): subprocess.TimeoutExpired(cmd=[], timeout=5),
            ("get", "pods"): subprocess.TimeoutExpired(cmd=[], timeout=5),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert result == []

    def test_returns_sorted(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({
            ("get", "ns", "-l"): (0, "deile-z deile-a deile-m"),
            ("get", "pods", "--all-namespaces"): (0, ""),
        })
        with patch("subprocess.run", side_effect=run):
            result = pd.discover_deile_namespaces()
        assert result == sorted(result)


# ===== _read_forge_kind =====================================================

class TestReadForgeKind:
    def test_returns_empty_when_kubectl_absent(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: None)
        assert pd._read_forge_kind("deile") == ""

    def test_returns_github_from_env(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        env_payload = json.dumps([
            {"name": "DEILE_FORGE_KIND", "value": "github"},
        ])
        run = _mock_run({("get", "deploy", "deile-pipeline"): (0, env_payload)})
        with patch("subprocess.run", side_effect=run):
            assert pd._read_forge_kind("deile") == "github"

    def test_returns_gitlab_from_env(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        env_payload = json.dumps([
            {"name": "OTHER_VAR", "value": "x"},
            {"name": "DEILE_FORGE_KIND", "value": "GitLab"},
        ])
        run = _mock_run({("get", "deploy", "deile-pipeline"): (0, env_payload)})
        with patch("subprocess.run", side_effect=run):
            assert pd._read_forge_kind("deile") == "gitlab"

    def test_returns_empty_when_var_absent(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        env_payload = json.dumps([{"name": "OTHER_VAR", "value": "x"}])
        run = _mock_run({("get", "deploy", "deile-pipeline"): (0, env_payload)})
        with patch("subprocess.run", side_effect=run):
            assert pd._read_forge_kind("deile") == ""

    def test_returns_empty_on_kubectl_error(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({("get", "deploy", "deile-pipeline"): (1, "")})
        with patch("subprocess.run", side_effect=run):
            assert pd._read_forge_kind("deile") == ""

    def test_returns_empty_on_bad_json(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")
        run = _mock_run({("get", "deploy", "deile-pipeline"): (0, "NOT_JSON")})
        with patch("subprocess.run", side_effect=run):
            assert pd._read_forge_kind("deile") == ""

    def test_returns_empty_on_timeout(self, monkeypatch):
        import subprocess
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/usr/bin/kubectl")

        def _raise(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=[], timeout=3)

        with patch("subprocess.run", side_effect=_raise):
            assert pd._read_forge_kind("deile") == ""


# ===== RuntimeContext com namespace dinâmico ================================

class TestRuntimeContextNamespace:
    def test_default_namespace_reads_from_NS(self, monkeypatch):
        monkeypatch.setattr(pd, "NS", "deile-custom")
        # Cria um contexto sem override de namespace; forge_kind mockado p/ ""
        with patch.object(pd, "_read_forge_kind", return_value=""):
            ctx = pd.RuntimeContext.detect()
        assert ctx.namespace == "deile-custom"

    def test_override_namespace(self, monkeypatch):
        with patch.object(pd, "_read_forge_kind", return_value=""):
            ctx = pd.RuntimeContext.detect(namespace="deile-gl")
        assert ctx.namespace == "deile-gl"

    def test_forge_kind_propagated_from_read(self, monkeypatch):
        with patch.object(pd, "_read_forge_kind", return_value="gitlab"):
            ctx = pd.RuntimeContext.detect(namespace="deile-gl")
        assert ctx.forge_kind == "gitlab"

    def test_forge_kind_override_skips_read(self, monkeypatch):
        # Quando forge_kind é passado como override, _read_forge_kind NÃO é chamado.
        called: list = []
        orig = pd._read_forge_kind

        def _spy(ns):
            called.append(ns)
            return orig(ns)

        with patch.object(pd, "_read_forge_kind", side_effect=_spy):
            ctx = pd.RuntimeContext.detect(forge_kind="github")
        assert ctx.forge_kind == "github"
        assert called == []  # não foi chamado
