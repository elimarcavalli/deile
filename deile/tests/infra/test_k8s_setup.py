"""Testes unitários do verbo ``k8s setup`` (issue #328).

Cobre o módulo ``infra/k8s/_setup.py``: validação de tokens, geração do
ConfigMap por NS, secrets shape, idempotência de namespace, fluxo
dry-run e modo --yes (que aborta o setup, por ser fundamentalmente
interativo). Subprocess sempre mockado — nenhum teste toca o cluster.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _setup as setup  # noqa: E402

# ============================================================================
# NamespacePlan
# ============================================================================

class TestNamespacePlanSecrets:
    def test_minimal_plan_has_required_secret_keys(self):
        plan = setup.NamespacePlan(
            name="deile",
            forge_kind="github",
            repo="owner/repo",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-test"},
            bot_bearer="bot-bearer-xxx",
            worker_bearer="worker-bearer-xxx",
        )
        bot, deile, worker = plan.secrets_kv()
        assert bot["DEILE_BOT_CONTROL_PLANE_AUTH_TOKEN"] == "bot-bearer-xxx"
        assert bot["ANTHROPIC_API_KEY"] == "sk-ant-test"
        assert "DEILE_BOT_DISCORD_TOKEN" not in bot  # bot opcional
        assert deile["DEILE_BOT_AUTH_TOKEN"] == "bot-bearer-xxx"
        assert deile["ANTHROPIC_API_KEY"] == "sk-ant-test"
        assert "GITHUB_TOKEN" not in deile
        assert "GITLAB_TOKEN" not in deile
        assert worker == {"AUTH_TOKEN": "worker-bearer-xxx"}

    def test_dual_forge_includes_both_tokens(self):
        plan = setup.NamespacePlan(
            name="deile-dual",
            forge_kind="auto",
            repo="owner/repo",
            dispatch_mode="deile_worker",
            llm_keys={"OPENAI_API_KEY": "sk-oai-x"},
            github_token="ghp_xxx",
            gitlab_token="glpat-yyy",
            bot_bearer="b",
            worker_bearer="w",
        )
        _, deile, _ = plan.secrets_kv()
        assert deile["GITHUB_TOKEN"] == "ghp_xxx"
        assert deile["GITLAB_TOKEN"] == "glpat-yyy"

    def test_discord_token_present_only_when_set(self):
        without = setup.NamespacePlan(
            name="a", forge_kind="github", repo="o/r", dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            bot_bearer="b", worker_bearer="w",
        )
        with_d = setup.NamespacePlan(
            name="b", forge_kind="github", repo="o/r", dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            discord_token="abc.def.ghi",
            bot_bearer="b", worker_bearer="w",
        )
        assert "DEILE_BOT_DISCORD_TOKEN" not in without.secrets_kv()[0]
        assert with_d.secrets_kv()[0]["DEILE_BOT_DISCORD_TOKEN"] == "abc.def.ghi"


class TestRuntimeConfigMap:
    def test_pipeline_settings_carries_overrides(self):
        plan = setup.NamespacePlan(
            name="deile-gl", forge_kind="gitlab",
            repo="group/sub/project", dispatch_mode="claude_subprocess",
            llm_keys={"DEEPSEEK_API_KEY": "sk-x"},
            bot_bearer="b", worker_bearer="w",
        )
        data = plan.runtime_configmap_data()
        # Estrutura completa
        assert sorted(data) == [
            "bot-settings.json",
            "oneshot-settings.json",
            "pipeline-settings.json",
            "shell-settings.json",
            "worker-settings.json",
        ]
        # Overrides chegam em pipeline-settings.json
        parsed = json.loads(data["pipeline-settings.json"])
        assert parsed["pipeline"]["dispatch_mode"] == "claude_subprocess"
        assert parsed["pipeline"]["repo"] == "group/sub/project"
        assert parsed["forge"]["kind"] == "gitlab"
        assert parsed["approval"]["auto"] is True
        # Outras seções continuam com defaults estáveis
        worker = json.loads(data["worker-settings.json"])
        assert worker["model"]["preferred"]  # algum modelo declarado
        assert worker["approval"]["auto"] is True

    def test_pipeline_settings_omits_forge_when_auto(self):
        """forge.kind=auto não vai pro JSON — deixa o detector decidir."""
        plan = setup.NamespacePlan(
            name="x", forge_kind="auto", repo="o/r", dispatch_mode="deile_worker",
            llm_keys={"OPENAI_API_KEY": "sk-x"},
            bot_bearer="b", worker_bearer="w",
        )
        parsed = json.loads(plan.runtime_configmap_data()["pipeline-settings.json"])
        assert "forge" not in parsed


# ============================================================================
# Token validation
# ============================================================================

class TestTokenValidation:
    @pytest.mark.parametrize("env_var,value,expected", [
        ("ANTHROPIC_API_KEY", "sk-ant-abcdefghijklmnopqrstu", True),
        ("ANTHROPIC_API_KEY", "sk-other-prefix-xxxxxxxxxxxx", False),
        ("ANTHROPIC_API_KEY", "", False),
        ("OPENAI_API_KEY", "sk-abcdefghijklmnopqrstu", True),
        ("DEEPSEEK_API_KEY", "sk-x", False),  # muito curto
        ("GITHUB_TOKEN", "ghp_abcdefghijklmnopqrstuv", True),
        ("GITHUB_TOKEN", "github_pat_11AAAAAAAA_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx", True),
        ("GITHUB_TOKEN", "glpat-mistake", False),  # prefixo errado
        ("GITLAB_TOKEN", "glpat-abcdefghijklmnop", True),
        ("GITLAB_TOKEN", "gldt-deploytoken1234567", True),
        ("GITLAB_TOKEN", "ghp_wrong-forge", False),
        ("DEILE_BOT_DISCORD_TOKEN", "abc.def-1.ghi_2", True),
        ("DEILE_BOT_DISCORD_TOKEN", "abc.def", False),  # falta 3o segmento
    ])
    def test_format_check(self, env_var, value, expected):
        assert setup._validate_token_format(env_var, value) is expected

    def test_unknown_env_var_passes_through(self):
        # Sem pattern registrado → aceita qualquer não-vazio.
        assert setup._validate_token_format("SOMETHING_NEW", "value") is True


# ============================================================================
# _ensure_namespace
# ============================================================================

class TestEnsureNamespace:
    def test_creates_when_missing_and_labels(self, monkeypatch):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            m = MagicMock()
            # 1ª chamada: get namespace → falha (ausente)
            # 2ª: create namespace → OK
            # 3ª: label → OK
            if len(calls) == 1 and "get" in cmd:
                m.returncode = 1
            else:
                m.returncode = 0
            m.stdout = ""
            m.stderr = ""
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        assert setup._ensure_namespace("kubectl", "deile-new",
                                       "app.kubernetes.io/managed-by=deile")
        assert any("create" in c and "namespace" in c for c in calls)
        # Labels PSS restricted + managed-by
        label_call = next(c for c in calls if "label" in c)
        assert "pod-security.kubernetes.io/enforce=restricted" in label_call
        assert "app.kubernetes.io/managed-by=deile" in label_call

    def test_idempotent_when_namespace_exists(self, monkeypatch):
        """NS já existe → pula `create`, só re-aplica labels."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0  # tudo OK
            m.stdout = m.stderr = ""
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        assert setup._ensure_namespace("kubectl", "deile",
                                       "app.kubernetes.io/managed-by=deile")
        commands = [" ".join(c) for c in calls]
        assert not any("create namespace" in c for c in commands)
        assert any("label" in c for c in commands)

    def test_create_failure_propagates(self, monkeypatch):
        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 1
            m.stdout = ""
            m.stderr = "AlreadyExists or quota error"
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        assert setup._ensure_namespace("kubectl", "deile-x",
                                       "app.kubernetes.io/managed-by=deile") is False


# ============================================================================
# _collect_namespace_plans com --yes
# ============================================================================

class TestCollectPlans:
    def test_yes_mode_aborts(self, capsys):
        """--yes não pode rodar (segredos via getpass exigem interativo)."""
        result = setup._collect_namespace_plans(yes=True, existing_ns=[])
        assert result == []
        captured = capsys.readouterr()
        assert "interativo" in captured.out + captured.err


# ============================================================================
# run_setup — fluxo dry_run
# ============================================================================

class TestRunSetupDryRun:
    def test_dry_run_short_circuits_after_plan(self, monkeypatch, tmp_path):
        """--dry-run imprime o plano e sai sem aplicar."""
        # Construir um plan stub e simular a coleta.
        plan_stub = setup.NamespacePlan(
            name="deile-dry", forge_kind="github", repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-test"},
            bot_bearer="b", worker_bearer="w",
        )
        monkeypatch.setattr(
            setup, "_collect_namespace_plans", lambda *_a, **_kw: [plan_stub]
        )
        # _ensure_kubernetes deve passar
        monkeypatch.setattr(
            setup, "_ensure_kubernetes", lambda *_a, **_kw: True
        )
        # apply_secret e _apply_namespace nunca devem ser chamados
        called = {"apply_secret": 0, "apply_ns": 0}

        def _no_secret(*a, **kw):
            called["apply_secret"] += 1
            return True

        rc = setup.run_setup(
            {"yes": False, "dry_run": True},
            kubectl_resolver=lambda: "/usr/bin/kubectl",
            cluster_reachable_fn=lambda: True,
            apply_secret_fn=_no_secret,
            discover_existing_fn=lambda: [],
            manifests_dir=tmp_path,
            setup_env_path=tmp_path / "setup_environment.py",
            deile_ns_label="app.kubernetes.io/managed-by=deile",
            deployments=("deilebot",),
        )
        assert rc == 0
        assert called["apply_secret"] == 0


# ============================================================================
# Wiring no deploy.py (smoke)
# ============================================================================

class TestDeployWiring:
    def test_setup_verb_registered(self):
        # Importa deploy.py via path do _setup já configurado nas linhas iniciais
        import deploy

        assert "setup" in deploy._K8S
        assert deploy._K8S["setup"] is deploy.k8s_setup
        action_names = [a for a, _ in deploy._K8S_ACTIONS]
        assert "setup" in action_names
