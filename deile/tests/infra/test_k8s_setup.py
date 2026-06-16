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
            name="a",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        with_d = setup.NamespacePlan(
            name="b",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            discord_token="abc.def.ghi",
            bot_bearer="b",
            worker_bearer="w",
        )
        assert "DEILE_BOT_DISCORD_TOKEN" not in without.secrets_kv()[0]
        assert with_d.secrets_kv()[0]["DEILE_BOT_DISCORD_TOKEN"] == "abc.def.ghi"


class TestRuntimeConfigMap:
    def test_pipeline_settings_carries_overrides(self):
        plan = setup.NamespacePlan(
            name="deile-gl",
            forge_kind="gitlab",
            repo="group/sub/project",
            dispatch_mode="claude_subprocess",
            llm_keys={"DEEPSEEK_API_KEY": "sk-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        data = plan.runtime_configmap_data()
        # Estrutura completa — issue #612: `pipeline.repo` é a FONTE ÚNICA do
        # repo-alvo (chave discreta), referenciada pelos manifests 46/55 via
        # configMapKeyRef; saiu do JSON pipeline-settings.json.
        assert sorted(data) == [
            "bot-settings.json",
            "oneshot-settings.json",
            "pipeline-settings.json",
            "pipeline.repo",
            "shell-settings.json",
            "worker-settings.json",
        ]
        # O repo-alvo vai na chave discreta, não mais no JSON.
        assert data["pipeline.repo"] == "group/sub/project"
        # Overrides chegam em pipeline-settings.json
        parsed = json.loads(data["pipeline-settings.json"])
        assert parsed["pipeline"]["dispatch_mode"] == "claude_subprocess"
        assert "repo" not in parsed["pipeline"]
        assert parsed["forge"]["kind"] == "gitlab"
        assert parsed["approval"]["auto"] is True
        # Outras seções continuam com defaults estáveis
        worker = json.loads(data["worker-settings.json"])
        assert worker["model"]["preferred"]  # algum modelo declarado
        assert worker["approval"]["auto"] is True

    def test_pipeline_settings_omits_forge_when_auto(self):
        """forge.kind=auto não vai pro JSON — deixa o detector decidir."""
        plan = setup.NamespacePlan(
            name="x",
            forge_kind="auto",
            repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"OPENAI_API_KEY": "sk-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        parsed = json.loads(plan.runtime_configmap_data()["pipeline-settings.json"])
        assert "forge" not in parsed


# ============================================================================
# Token validation
# ============================================================================


class TestTokenValidation:
    @pytest.mark.parametrize(
        "env_var,value,expected",
        [
            ("ANTHROPIC_API_KEY", "sk-ant-abcdefghijklmnopqrstu", True),
            ("ANTHROPIC_API_KEY", "sk-other-prefix-xxxxxxxxxxxx", False),
            ("ANTHROPIC_API_KEY", "", False),
            ("OPENAI_API_KEY", "sk-abcdefghijklmnopqrstu", True),
            ("DEEPSEEK_API_KEY", "sk-x", False),  # muito curto
            ("GITHUB_TOKEN", "ghp_abcdefghijklmnopqrstuv", True),
            (
                "GITHUB_TOKEN",
                "github_pat_11AAAAAAAA_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
                True,
            ),
            ("GITHUB_TOKEN", "glpat-mistake", False),  # prefixo errado
            ("GITLAB_TOKEN", "glpat-abcdefghijklmnop", True),
            ("GITLAB_TOKEN", "gldt-deploytoken1234567", True),
            ("GITLAB_TOKEN", "ghp_wrong-forge", False),
            ("DEILE_BOT_DISCORD_TOKEN", "abc.def-1.ghi_2", True),
            ("DEILE_BOT_DISCORD_TOKEN", "abc.def", False),  # falta 3o segmento
        ],
    )
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
        assert setup._ensure_namespace(
            "kubectl", "deile-new", "app.kubernetes.io/managed-by=deile"
        )
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
        assert setup._ensure_namespace(
            "kubectl", "deile", "app.kubernetes.io/managed-by=deile"
        )
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
        assert (
            setup._ensure_namespace(
                "kubectl", "deile-x", "app.kubernetes.io/managed-by=deile"
            )
            is False
        )


# ============================================================================
# _apply_runtime_configmap
# ============================================================================


class TestApplyRuntimeConfigmap:
    def test_renders_then_applies_with_label(self, monkeypatch):
        """Renderiza CM via --dry-run=client + apply via stdin + label."""
        calls = []

        def fake_run(cmd, **kw):
            calls.append(list(cmd))
            m = MagicMock()
            m.returncode = 0
            m.stdout = "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
            m.stderr = ""
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        plan = setup.NamespacePlan(
            name="deile-x",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        assert setup._apply_runtime_configmap("kubectl", "deile-x", plan) is True
        # 3 chamadas: render (create cm --dry-run), apply -f -, label
        assert any("create" in c and "configmap" in c for c in calls)
        assert any("apply" in c and "-f" in c for c in calls)
        assert any("label" in c and "configmap" in c for c in calls)
        # E o label aplica app=deile
        label_call = next(c for c in calls if "label" in c and "configmap" in c)
        assert "app=deile" in label_call

    def test_label_failure_is_warning_not_fatal(self, monkeypatch, capsys):
        """Falha do `kubectl label configmap` é warning, não fatal — o CM
        funcional já foi aplicado, label é só para filtros do panel."""
        call_idx = [0]

        def fake_run(cmd, **kw):
            call_idx[0] += 1
            m = MagicMock()
            m.stdout = "yaml-rendered" if call_idx[0] == 1 else ""
            # Render OK, apply OK, label FALHA
            if "label" in cmd and "configmap" in cmd:
                m.returncode = 1
                m.stderr = "forbidden"
            else:
                m.returncode = 0
                m.stderr = ""
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        plan = setup.NamespacePlan(
            name="deile-y",
            forge_kind="gitlab",
            repo="g/p",
            dispatch_mode="deile_worker",
            llm_keys={"OPENAI_API_KEY": "sk-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        assert setup._apply_runtime_configmap("kubectl", "deile-y", plan) is True
        captured = capsys.readouterr()
        assert "label" in captured.out.lower() or "label" in captured.err.lower()

    def test_render_failure_propagates(self, monkeypatch):
        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 1 if "create" in cmd and "configmap" in cmd else 0
            m.stdout = ""
            m.stderr = "invalid"
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        plan = setup.NamespacePlan(
            name="x",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            llm_keys={"ANTHROPIC_API_KEY": "sk-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        assert setup._apply_runtime_configmap("kubectl", "x", plan) is False


# ============================================================================
# _wait_for_pods_ready + _deployments_to_wait_for
# ============================================================================


class TestWaitForPodsReady:
    def test_skips_deployment_when_not_present(self, monkeypatch):
        """Deployment ausente é pulado — não conta como falha."""

        def fake_run(cmd, **kw):
            m = MagicMock()
            # `get deployment` → exists=1 (ausente)
            m.returncode = 1 if "get" in cmd and "deployment" in cmd else 0
            m.stdout = m.stderr = ""
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        assert (
            setup._wait_for_pods_ready("kubectl", "deile", ("nope-dep",), timeout_s=1)
            is True
        )

    def test_rollout_failure_is_non_fatal_but_returns_false(self, monkeypatch):
        """`rollout status` falhando despeja logs e retorna False (não levanta)."""

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.stdout = m.stderr = ""
            if "get" in cmd and "deployment" in cmd:
                m.returncode = 0  # presente
            elif "rollout" in cmd:
                m.returncode = 1  # não ficou Ready
            else:
                m.returncode = 0  # logs
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        assert (
            setup._wait_for_pods_ready(
                "kubectl", "deile", ("deile-worker",), timeout_s=1
            )
            is False
        )


class TestDeploymentsToWaitFor:
    def test_bot_disabled_omits_deilebot(self):
        plan = setup.NamespacePlan(
            name="x",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=False,
            llm_keys={"OPENAI_API_KEY": "sk-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        base = ("deilebot", "deile-worker", "deile-shell", "deile-pipeline")
        result = setup._deployments_to_wait_for(plan, base)
        assert "deilebot" not in result
        assert set(result) == {"deile-worker", "deile-shell", "deile-pipeline"}

    def test_bot_enabled_keeps_deilebot(self):
        plan = setup.NamespacePlan(
            name="x",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=True,
            llm_keys={"OPENAI_API_KEY": "sk-x"},
            discord_token="a.b.c",
            bot_bearer="b",
            worker_bearer="w",
        )
        base = ("deilebot", "deile-worker")
        assert setup._deployments_to_wait_for(plan, base) == base


# ============================================================================
# _apply_namespace — bot opt-out + sequência de manifests
# ============================================================================


class TestApplyNamespace:
    def test_bot_opt_out_skips_bot_manifests_and_bot_secret(
        self, monkeypatch, tmp_path
    ):
        """Sem bot habilitado, manifests do bot e `bot-secrets` são pulados."""
        applied_manifests = []
        applied_secrets = []

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = m.stderr = ""
            if "apply" in cmd and "-f" in cmd:
                # Captura o último item — o caminho do manifest
                manifest_path = cmd[-1]
                applied_manifests.append(Path(manifest_path).name)
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        monkeypatch.setattr(setup, "_ensure_namespace", lambda *_a, **_kw: True)
        monkeypatch.setattr(setup, "_apply_runtime_configmap", lambda *_a, **_kw: True)

        def fake_apply_secret(kubectl, name, kv, ns=""):
            applied_secrets.append(name)
            return True

        plan = setup.NamespacePlan(
            name="deile-no-bot",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=False,
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            bot_bearer="b",
            worker_bearer="w",
        )
        ok = setup._apply_namespace(
            "kubectl",
            plan,
            fake_apply_secret,
            tmp_path,
            "app.kubernetes.io/managed-by=deile",
        )
        assert ok is True
        # bot-secrets NUNCA aplicado
        assert "bot-secrets" not in applied_secrets
        # mas deile-secrets e worker-bearer sim
        assert "deile-secrets" in applied_secrets
        assert "worker-bearer" in applied_secrets
        # PVC + Deployment do bot NUNCA aplicados
        assert "19-bot-data-pvc.yaml" not in applied_manifests
        assert "20-bot-deployment.yaml" not in applied_manifests
        # MAS ``15-bot-config.yaml`` (ConfigMap compartilhado) AINDA aplicado
        # — worker (45) e shell (35) o montam para `clonable_repos`.
        assert "15-bot-config.yaml" in applied_manifests
        # E worker, shell e pipeline sim
        assert "35-deile-interactive.yaml" in applied_manifests
        assert "45-deile-worker-deployment.yaml" in applied_manifests
        assert "46-deile-pipeline-deployment.yaml" in applied_manifests

    def test_bot_enabled_applies_all_manifests_and_bot_secret(
        self, monkeypatch, tmp_path
    ):
        applied_manifests = []
        applied_secrets = []

        def fake_run(cmd, **kw):
            m = MagicMock()
            m.returncode = 0
            m.stdout = m.stderr = ""
            if "apply" in cmd and "-f" in cmd:
                applied_manifests.append(Path(cmd[-1]).name)
            return m

        monkeypatch.setattr(setup.subprocess, "run", fake_run)
        monkeypatch.setattr(setup, "_ensure_namespace", lambda *_a, **_kw: True)
        monkeypatch.setattr(setup, "_apply_runtime_configmap", lambda *_a, **_kw: True)

        def fake_apply_secret(kubectl, name, kv, ns=""):
            applied_secrets.append(name)
            return True

        plan = setup.NamespacePlan(
            name="deile-full",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=True,
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-x"},
            discord_token="a.b.c",
            bot_bearer="b",
            worker_bearer="w",
        )
        ok = setup._apply_namespace(
            "kubectl",
            plan,
            fake_apply_secret,
            tmp_path,
            "app.kubernetes.io/managed-by=deile",
        )
        assert ok is True
        assert "bot-secrets" in applied_secrets
        assert "20-bot-deployment.yaml" in applied_manifests


# ============================================================================
# run_setup — early reject + dry-run + partial-state guidance
# ============================================================================


class TestRunSetupYesRejection:
    def test_yes_at_top_returns_2_without_calling_anything(self, capsys, tmp_path):
        """`--yes` é rejeitado no topo de run_setup — F1 nem roda."""
        called = {"ensure_k8s": 0, "collect": 0, "apply": 0}

        def _track_ek(*_a, **_kw):
            called["ensure_k8s"] += 1
            return True

        def _track_col(*_a, **_kw):
            called["collect"] += 1
            return []

        rc = setup.run_setup(
            {"yes": True, "dry_run": False},
            kubectl_resolver=lambda: "/usr/bin/kubectl",
            cluster_reachable_fn=lambda: True,
            apply_secret_fn=lambda *a, **kw: True,
            discover_existing_fn=lambda: [],
            manifests_dir=tmp_path,
            setup_env_path=tmp_path / "x",
            deile_ns_label="app.kubernetes.io/managed-by=deile",
            deployments=("deilebot",),
        )
        assert rc == 2
        captured = capsys.readouterr()
        assert "interativo" in captured.out + captured.err


class TestRunSetupDryRun:
    def test_dry_run_short_circuits_after_plan(self, monkeypatch, tmp_path):
        """--dry-run imprime o plano e sai sem aplicar."""
        plan_stub = setup.NamespacePlan(
            name="deile-dry",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=False,
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-test"},
            bot_bearer="b",
            worker_bearer="w",
        )
        monkeypatch.setattr(
            setup, "_collect_namespace_plans", lambda *_a, **_kw: [plan_stub]
        )
        # _ensure_kubernetes não deve sequer ser chamado em dry-run
        ensure_called = {"n": 0}

        def _ek(*_a, **_kw):
            ensure_called["n"] += 1
            return True

        monkeypatch.setattr(setup, "_ensure_kubernetes", _ek)
        called = {"apply_secret": 0}

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
        assert ensure_called["n"] == 0


# ============================================================================
# _validate — usa filtro de deployments por bot
# ============================================================================


class TestValidate:
    def test_validate_skips_deilebot_when_bot_disabled(self, monkeypatch):
        """`_validate` chama `_wait_for_pods_ready` com `deilebot` removido
        quando o plan tem ``bot_enabled=False``."""
        wait_calls = []

        def fake_wait(kubectl, ns, deployments, timeout_s):
            wait_calls.append(list(deployments))
            return True

        monkeypatch.setattr(setup, "_wait_for_pods_ready", fake_wait)
        # Mock subprocess.run para o get pods,deployments,services final
        monkeypatch.setattr(
            setup.subprocess, "run", lambda *a, **kw: MagicMock(returncode=0)
        )

        plans = [
            setup.NamespacePlan(
                name="ns-bot",
                forge_kind="github",
                repo="o/r",
                dispatch_mode="deile_worker",
                bot_enabled=True,
                llm_keys={"ANTHROPIC_API_KEY": "sk-x"},
                discord_token="a.b.c",
                bot_bearer="b",
                worker_bearer="w",
            ),
            setup.NamespacePlan(
                name="ns-no-bot",
                forge_kind="github",
                repo="o/r",
                dispatch_mode="deile_worker",
                bot_enabled=False,
                llm_keys={"ANTHROPIC_API_KEY": "sk-x"},
                bot_bearer="b",
                worker_bearer="w",
            ),
        ]
        base_deps = ("deilebot", "deile-worker", "deile-pipeline")
        setup._validate("kubectl", plans, base_deps, timeout_s=1)
        assert "deilebot" in wait_calls[0]
        assert "deilebot" not in wait_calls[1]


# ============================================================================
# NamespacePlan repr — não vaza secrets
# ============================================================================


class TestPlanRepr:
    def test_repr_omits_sensitive_fields(self):
        plan = setup.NamespacePlan(
            name="x",
            forge_kind="github",
            repo="o/r",
            dispatch_mode="deile_worker",
            bot_enabled=True,
            llm_keys={"ANTHROPIC_API_KEY": "sk-ant-SUPER-SECRET-VALUE-XYZ"},
            github_token="ghp_SHOULD-NOT-LEAK-ABC",
            gitlab_token="glpat-SHOULD-NOT-LEAK-DEF",
            discord_token="aaa.bbb.ccc-DISCORD-SECRET",
            bot_bearer="bot-bearer-SECRET",
            worker_bearer="worker-bearer-SECRET",
        )
        r = repr(plan)
        # Campos seguros aparecem
        assert "name=" in r
        assert "forge_kind=" in r
        assert "repo=" in r
        assert "bot_enabled=" in r
        # Nenhum segredo vaza
        assert "SUPER-SECRET" not in r
        assert "SHOULD-NOT-LEAK" not in r
        assert "DISCORD-SECRET" not in r
        assert "bot-bearer-SECRET" not in r
        assert "worker-bearer-SECRET" not in r


# ============================================================================
# Wiring no deploy.py (smoke)
# ============================================================================


class TestDeployWiring:
    def test_setup_verb_registered(self):
        import deploy

        assert "setup" in deploy._K8S
        assert deploy._K8S["setup"] is deploy.k8s_setup
        action_names = [a for a, _ in deploy._K8S_ACTIONS]
        assert "setup" in action_names
