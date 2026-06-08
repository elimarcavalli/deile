"""Fase 6 (multi-CLI) — INFRA on-demand: gen-worker, build-cli-workers, NetworkPolicy.

Prova os entregáveis da fase + a INVARIANTE CRÍTICA do plano §1.0:

* ``k8s up`` (qualquer perfil) NÃO referencia nenhum CLI worker — a frota é
  100%% opt-in, instalada/escalada sob demanda (réplicas nascem 0). Nenhum CLI
  worker é obrigatório para a stack subir.
* ``gen-worker`` renderiza o manifest de um worker do TEMPLATE a partir dos
  METADADOS do adapter (porta, env de auth, storage, egress) — não YAML à mão.
* A NetworkPolicy é GERADA dos ``egress_hosts`` do adapter + forges.
* ``build-cli-workers`` usa ``Dockerfile.cli-worker`` com build-arg ``WORKER_KIND``.
* O painel deriva os model-ids de um CLI worker do registro (mesma fonte do
  ``GET /v1/models``).

O pacote ``cli_adapters`` + ``_cli_worker_gen`` vivem em ``infra/k8s/`` — path
inserido manualmente (convenção dos testes de infra).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
import yaml

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _cli_worker_gen as gen  # noqa: E402
import cli_adapters  # noqa: E402
import deploy  # noqa: E402

_FLEET_KINDS = sorted(cli_adapters.ADAPTERS)


# ===== INVARIANTE CRÍTICA — k8s up NÃO exige/instala nenhum CLI worker ========


class TestKsUpNeverReferencesCliWorkers:
    """A frota CLI é opt-in: nenhum perfil de ``k8s up`` a menciona."""

    @pytest.mark.parametrize("profile_name", deploy.DeploymentProfile.VALID)
    def test_profile_manifests_have_no_cli_worker(self, profile_name):
        profile = deploy.DeploymentProfile(profile_name)
        manifests = " ".join(profile.manifests)
        for kind in _FLEET_KINDS:
            assert f"{kind}-worker" not in manifests, (
                f"perfil {profile_name!r} referencia {kind}-worker nos manifests "
                "— a frota CLI deve ser 100% opt-in, fora do k8s up"
            )
        # Também não pode haver um manifest genérico de cli-worker no perfil.
        assert "cli-worker" not in manifests
        assert "cli_worker" not in manifests

    @pytest.mark.parametrize("profile_name", deploy.DeploymentProfile.VALID)
    def test_profile_deployments_have_no_cli_worker(self, profile_name):
        profile = deploy.DeploymentProfile(profile_name)
        for dep in profile.deployments:
            assert dep in (
                "deilebot", "deile-worker", "deile-shell",
                "deile-pipeline", "claude-worker", "deile-monitor",
            ), f"perfil {profile_name!r} sobe deployment inesperado: {dep!r}"
            for kind in _FLEET_KINDS:
                assert dep != f"{kind}-worker"

    def test_k8s_deployments_tuple_has_no_cli_worker(self):
        """``K8S_DEPLOYMENTS`` (start/stop/restart) não inclui CLI workers."""
        for kind in _FLEET_KINDS:
            assert f"{kind}-worker" not in deploy.K8S_DEPLOYMENTS

    def test_k8s_up_dry_run_does_not_reference_cli_workers(self, monkeypatch, capsys):
        """``k8s up --dry-run`` imprime o plano SEM nenhum CLI worker.

        Exercita o caminho real do verb (announce_plan retorna False em dry-run,
        então ``k8s_up`` retorna 0 sem tocar o cluster) e prova que nada na saída
        menciona um worker da frota CLI.
        """
        rc = deploy.k8s_up({"dry_run": True, "yes": True, "extra": []})
        assert rc == 0
        out = capsys.readouterr().out
        for kind in _FLEET_KINDS:
            assert f"{kind}-worker" not in out, (
                f"k8s up dry-run mencionou {kind}-worker — frota não é opt-in"
            )


# ===== gen-worker — manifest derivado do template + metadados do adapter ======


def _needs_pvc(adapter) -> bool:
    return adapter.auth_mode == "oauth_file" or adapter.supports_resume


class TestGenWorkerRender:
    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_renders_core_docs(self, kind):
        """Os 5 docs base sempre saem, na ordem; PVC workers ganham +2 (PVC+Cron).

        São DUAS NetworkPolicies: a do worker (ingress do pipeline + egress
        LLM/forge) e a ``pipeline-egress-to-<worker>`` (abre o egress do pipeline
        para este worker — netpol é aplicada nas duas pontas e o pipeline tem
        egress restritivo por default-deny). Workers ``emptyDir`` (env-only) = 5
        docs; workers com PVC (``oauth_file``/``supports_resume``) = 7 (acrescentam
        ``PersistentVolumeClaim`` + ``CronJob`` de cleanup).
        """
        rendered = gen.render_manifests(kind, namespace="deile")
        kinds = [d["kind"] for d in yaml.safe_load_all(rendered) if d]
        assert kinds[:5] == [
            "Deployment", "Service", "Secret", "NetworkPolicy", "NetworkPolicy",
        ]
        adapter = cli_adapters.ADAPTERS[kind]
        if _needs_pvc(adapter):
            assert kinds == [
                "Deployment", "Service", "Secret", "NetworkPolicy", "NetworkPolicy",
                "PersistentVolumeClaim", "CronJob",
            ]
        else:
            assert kinds == [
                "Deployment", "Service", "Secret", "NetworkPolicy", "NetworkPolicy",
            ]

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_deployment_is_scale_to_zero(self, kind):
        docs = list(yaml.safe_load_all(gen.render_manifests(kind)))
        dep = next(d for d in docs if d and d["kind"] == "Deployment")
        assert dep["spec"]["replicas"] == 0, (
            "CLI worker deve nascer scale-to-zero (opt-in, custo zero ocioso)"
        )

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_port_and_image_derived_from_adapter(self, kind):
        adapter = cli_adapters.ADAPTERS[kind]
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        svc = next(d for d in docs if d["kind"] == "Service")
        assert svc["spec"]["ports"][0]["port"] == adapter.default_port
        dep = next(d for d in docs if d["kind"] == "Deployment")
        img = dep["spec"]["template"]["spec"]["containers"][0]["image"]
        assert img == f"deile-cli-worker-{kind}:local"

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_auth_keys_come_from_shared_secret_not_literal(self, kind):
        """As ``auth_env_keys`` são lidas do Secret ``cli-worker-keys``, nunca literais."""
        adapter = cli_adapters.ADAPTERS[kind]
        rendered = gen.render_manifests(kind)
        for key in adapter.auth_env_keys:
            assert key in rendered
            # Aparece como secretKeyRef do Secret compartilhado, não como value.
            assert "cli-worker-keys" in rendered

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_storage_mode_matches_adapter(self, kind):
        adapter = cli_adapters.ADAPTERS[kind]
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        vols = {v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]}
        home = vols["worker-home"]
        needs_pvc = adapter.auth_mode == "oauth_file" or adapter.supports_resume
        if needs_pvc:
            assert "persistentVolumeClaim" in home
        else:
            assert "emptyDir" in home

    def test_unknown_kind_raises(self):
        with pytest.raises(KeyError):
            gen.render_manifests("nonexistent-cli")

    def test_write_manifests_creates_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr(gen, "GENERATED_DIR", tmp_path)
        kind = _FLEET_KINDS[0]
        out = gen.write_manifests(kind, namespace="deile")
        assert out.is_file()
        docs = [d for d in yaml.safe_load_all(out.read_text()) if d]
        # _FLEET_KINDS[0] é env-only (4 docs); o caso PVC tem teste dedicado.
        assert len(docs) >= 4


class TestPvcWorkerGeneratesPvcAndCron:
    """Finding 4 (regressão): worker com PVC gera o objeto PVC + CronJob de GC.

    Um worker ``oauth_file``/``supports_resume`` referencia
    ``persistentVolumeClaim.claimName: <worker>-home`` no Deployment; sem emitir
    o objeto PVC a claim ficaria unbound e o Pod travaria em ``Pending``. O
    template tem de emitir a PVC (e o CronJob de cleanup que a monta).
    """

    @pytest.fixture
    def pvc_kind(self):
        from cli_adapters.base import BaseCliAdapter, ModelInfo, WorkResult

        class _PvcAdapter(BaseCliAdapter):
            def build_argv(self, **_kw):
                return ["true"]

            def parse_output(self, **_kw):
                return WorkResult(ok=True)

            def list_models(self):
                return [ModelInfo(id="x")]

        kind = "pvcprobe"
        cli_adapters.ADAPTERS[kind] = _PvcAdapter(
            kind=kind, default_port=8798, auth_mode="oauth_file",
            supports_resume=True,
        )
        try:
            yield kind
        finally:
            cli_adapters.ADAPTERS.pop(kind, None)

    def test_pvc_object_emitted_and_matches_claim(self, pvc_kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(pvc_kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        home = next(
            v for v in dep["spec"]["template"]["spec"]["volumes"]
            if v["name"] == "worker-home"
        )
        claim = home["persistentVolumeClaim"]["claimName"]
        pvc = next(d for d in docs if d["kind"] == "PersistentVolumeClaim")
        assert pvc["metadata"]["name"] == claim, (
            "o claimName referenciado no Deployment deve ter um objeto PVC gerado"
        )
        assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]

    def test_cleanup_cronjob_emitted_for_pvc_worker(self, pvc_kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(pvc_kind)) if d]
        cron = next(d for d in docs if d["kind"] == "CronJob")
        spec = cron["spec"]["jobTemplate"]["spec"]["template"]["spec"]
        vol = next(v for v in spec["volumes"] if v["name"] == "worker-home")
        assert vol["persistentVolumeClaim"]["claimName"] == f"{pvc_kind}-worker-home"
        # O CronJob chama o cleanup do core via cli_worker_server.run_cleanup.
        args = spec["containers"][0]["args"][0]
        assert "run_cleanup" in args

    def test_env_only_worker_has_no_pvc_no_cron(self):
        # Sanity: um worker env-only não emite PVC nem CronJob.
        env_kind = next(
            k for k in _FLEET_KINDS if not _needs_pvc(cli_adapters.ADAPTERS[k])
        )
        kinds = [
            d["kind"] for d in yaml.safe_load_all(gen.render_manifests(env_kind)) if d
        ]
        assert "PersistentVolumeClaim" not in kinds
        assert "CronJob" not in kinds


# ===== OAuth bootstrap-creds initContainer (workers oauth_file) ================


class TestOauthInitContainerGeneration:
    """Workers ``auth_mode="oauth_file"`` ganham o initContainer ``bootstrap-creds``
    (espelha o claude-worker manifest 50) + o volume do Secret de credencial.

    Workers ``env`` NÃO geram initContainer (no-op, sem regressão). O bloco é
    gerado condicionalmente, exatamente como o PVC já é.
    """

    @pytest.fixture
    def oauth_kind(self):
        from cli_adapters.base import (BaseCliAdapter, ModelInfo, OAuthSpec,
                                       WorkResult)

        class _OauthAdapter(BaseCliAdapter):
            def build_argv(self, **_kw):
                return ["true"]

            def parse_output(self, **_kw):
                return WorkResult(ok=True)

            def list_models(self):
                return [ModelInfo(id="x")]

        kind = "oauthprobe"
        cli_adapters.ADAPTERS[kind] = _OauthAdapter(
            kind=kind, default_port=8799, auth_mode="oauth_file",
            oauth=OAuthSpec(
                cred_path="~/.oauthprobe/auth.json",
                login_cmd=["oauthprobe", "login", "--device-auth"],
                secret_name="oauthprobe-credentials",
            ),
        )
        try:
            yield kind
        finally:
            cli_adapters.ADAPTERS.pop(kind, None)

    def test_yaml_is_valid_with_init_block(self, oauth_kind):
        # safe_load_all estoura se o block-scalar do script ficar mal indentado.
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(oauth_kind)) if d]
        assert any(d["kind"] == "Deployment" for d in docs)

    def test_initcontainer_bootstrap_creds_emitted(self, oauth_kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(oauth_kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        inits = dep["spec"]["template"]["spec"].get("initContainers") or []
        names = [c["name"] for c in inits]
        assert "bootstrap-creds" in names
        ic = next(c for c in inits if c["name"] == "bootstrap-creds")
        assert ic["image"] == f"deile-cli-worker-{oauth_kind}:local"
        # Copia para mode 0600 (paridade com claude-worker).
        assert "0600" in ic["args"][0]

    def test_initcontainer_mounts_secret_and_pvc(self, oauth_kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(oauth_kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        ic = next(
            c for c in dep["spec"]["template"]["spec"]["initContainers"]
            if c["name"] == "bootstrap-creds"
        )
        mounts = {m["name"]: m["mountPath"] for m in ic["volumeMounts"]}
        assert mounts["oauth-cred"] == f"/run/secrets/{oauth_kind}-oauth"
        assert mounts["worker-home"] == f"/home/{oauth_kind}"

    def test_oauth_cred_secret_volume_present(self, oauth_kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(oauth_kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        vols = {
            v["name"]: v for v in dep["spec"]["template"]["spec"]["volumes"]
        }
        assert "oauth-cred" in vols
        # O nome do Secret vem do OAuthSpec.secret_name do adapter.
        assert vols["oauth-cred"]["secret"]["secretName"] == "oauthprobe-credentials"

    def test_resolve_pod_cred_path_expands_home(self, oauth_kind):
        adapter = cli_adapters.ADAPTERS[oauth_kind]
        path = gen.resolve_pod_cred_path(adapter, kind=oauth_kind)
        assert path == f"/home/{oauth_kind}/.oauthprobe/auth.json"

    def test_cred_secret_name_falls_back_when_undeclared(self):
        from cli_adapters.base import BaseCliAdapter, OAuthSpec

        class _A(BaseCliAdapter):
            pass

        adapter = _A(
            kind="nosecret", default_port=1, auth_mode="oauth_file",
            oauth=OAuthSpec(
                cred_path="~/.nosecret/auth.json",
                login_cmd=["x"], secret_name="",
            ),
        )
        assert gen.cred_secret_name(adapter, kind="nosecret") == (
            "nosecret-worker-credentials"
        )

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_env_workers_have_no_initcontainer(self, kind):
        adapter = cli_adapters.ADAPTERS[kind]
        if adapter.auth_mode == "oauth_file":
            pytest.skip("kind oauth_file — coberto pelos testes dedicados")
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        assert dep["spec"]["template"]["spec"].get("initContainers") in (None, [])
        vols = [v["name"] for v in dep["spec"]["template"]["spec"]["volumes"]]
        assert "oauth-cred" not in vols


# ===== oauth_mode override — adapter env-default oauth-capable (codex) =========


class TestOauthModeOverride:
    """``render_manifests(kind, oauth_mode=True)`` força o caminho OAuth num
    adapter cujo ``auth_mode`` default é ``env`` mas que é oauth-capable (codex):
    PVC + initContainer ``bootstrap-creds`` + mount da credencial + env
    ``DEILE_<KIND>_AUTH=oauth``. Sem o flag (default), o codex renderiza inalterado
    (``emptyDir``, sem initContainer, sem o env) — nenhuma regressão.
    """

    def _docs(self, rendered):
        return [d for d in yaml.safe_load_all(rendered) if d]

    def _container_env(self, dep):
        return {
            e["name"]: e
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
        }

    def test_codex_oauth_mode_renders_oauth_blocks(self):
        rendered = gen.render_manifests("codex", namespace="deile", oauth_mode=True)
        docs = self._docs(rendered)
        kinds = [d["kind"] for d in docs]
        # PVC + CronJob aparecem (worker passa a persistir estado).
        assert "PersistentVolumeClaim" in kinds
        assert "CronJob" in kinds
        dep = next(d for d in docs if d["kind"] == "Deployment")
        spec = dep["spec"]["template"]["spec"]
        # initContainer bootstrap-creds presente.
        inits = [c["name"] for c in (spec.get("initContainers") or [])]
        assert "bootstrap-creds" in inits
        # volume da credencial OAuth presente.
        vols = {v["name"]: v for v in spec["volumes"]}
        assert "oauth-cred" in vols
        assert "persistentVolumeClaim" in vols["worker-home"]
        # mount da auth.json: o cred path do codex é ~/.codex/auth.json.
        ic = next(c for c in spec["initContainers"] if c["name"] == "bootstrap-creds")
        assert "/home/codex/.codex/auth.json" in ic["args"][0]
        # env DEILE_CODEX_AUTH=oauth presente.
        env = self._container_env(dep)
        assert env["DEILE_CODEX_AUTH"]["value"] == "oauth"

    def test_codex_default_unchanged_no_oauth_blocks(self):
        rendered = gen.render_manifests("codex", namespace="deile")
        docs = self._docs(rendered)
        kinds = [d["kind"] for d in docs]
        assert "PersistentVolumeClaim" not in kinds
        assert "CronJob" not in kinds
        dep = next(d for d in docs if d["kind"] == "Deployment")
        spec = dep["spec"]["template"]["spec"]
        assert spec.get("initContainers") in (None, [])
        vols = {v["name"]: v for v in spec["volumes"]}
        assert "oauth-cred" not in vols
        assert "emptyDir" in vols["worker-home"]
        env = self._container_env(dep)
        assert "DEILE_CODEX_AUTH" not in env

    def test_codex_oauth_mode_yaml_is_valid(self):
        # safe_load_all estoura se o block-scalar do initContainer ficar mal
        # indentado; este teste prova que o YAML do modo OAuth é parseável.
        docs = self._docs(
            gen.render_manifests("codex", oauth_mode=True)
        )
        assert any(d["kind"] == "Deployment" for d in docs)

    def test_oauth_file_adapter_renders_oauth_without_flag(self):
        # Sanity: um adapter auth_mode=oauth_file renderiza OAuth mesmo SEM o flag
        # (oauth_mode default False) — o override não regride o caminho estático.
        from cli_adapters.base import (BaseCliAdapter, ModelInfo, OAuthSpec,
                                       WorkResult)

        class _A(BaseCliAdapter):
            def build_argv(self, **_kw):
                return ["true"]

            def parse_output(self, **_kw):
                return WorkResult(ok=True)

            def list_models(self):
                return [ModelInfo(id="x")]

        kind = "staticoauth"
        cli_adapters.ADAPTERS[kind] = _A(
            kind=kind, default_port=8797, auth_mode="oauth_file",
            oauth=OAuthSpec(
                cred_path="~/.staticoauth/auth.json",
                login_cmd=["x", "login"], secret_name="staticoauth-credentials",
            ),
        )
        try:
            docs = self._docs(gen.render_manifests(kind))  # sem oauth_mode
            dep = next(d for d in docs if d["kind"] == "Deployment")
            inits = [
                c["name"]
                for c in (dep["spec"]["template"]["spec"].get("initContainers") or [])
            ]
            assert "bootstrap-creds" in inits
            # adapter oauth_file também emite DEILE_<KIND>_AUTH=oauth.
            env = self._container_env(dep)
            assert env["DEILE_STATICOAUTH_AUTH"]["value"] == "oauth"
        finally:
            cli_adapters.ADAPTERS.pop(kind, None)


# ===== NetworkPolicy gerada dos egress_hosts do adapter =======================


class TestNetworkPolicyGeneration:
    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_egress_hosts_include_adapter_and_forges(self, kind):
        adapter = cli_adapters.ADAPTERS[kind]
        hosts = gen.egress_hosts(adapter)
        for h in adapter.egress_hosts:
            assert h in hosts
        assert "github.com" in hosts
        assert "gitlab.com" in hosts

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_netpol_ingress_only_from_pipeline(self, kind):
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        np = next(d for d in docs if d["kind"] == "NetworkPolicy")
        ingress = np["spec"]["ingress"]
        froms = ingress[0]["from"]
        assert froms == [{"podSelector": {"matchLabels": {"app": "deile-pipeline"}}}]

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_netpol_documents_egress_hosts_in_annotation(self, kind):
        adapter = cli_adapters.ADAPTERS[kind]
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        np = next(d for d in docs if d["kind"] == "NetworkPolicy")
        ann = np["metadata"]["annotations"]["deile.io/egress-llm-hosts"]
        for h in adapter.egress_hosts:
            assert h in ann

    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_pipeline_egress_netpol_opens_route_to_worker(self, kind):
        """Regressão (homologação E2E): o template DEVE gerar a netpol do lado do
        pipeline abrindo egress para este worker.

        Sem ela, o ``default-deny-all`` + as regras estáticas de egress do pipeline
        (só claude-worker/deile-worker) bloqueiam o pacote na origem — a ingress do
        worker sozinha não basta (netpol é aplicada nas DUAS pontas). Foi o blocker
        real do opencode na homologação: pod 1/1 Running, server escutando 0.0.0.0,
        /v1/health 200 in-pod, mas pipeline→worker falhava em 0ms.
        """
        worker = f"{kind}-worker"
        docs = [d for d in yaml.safe_load_all(gen.render_manifests(kind)) if d]
        nps = [d for d in docs if d["kind"] == "NetworkPolicy"]
        egress_np = next(
            (d for d in nps if d["metadata"]["name"] == f"pipeline-egress-to-{worker}"),
            None,
        )
        assert egress_np is not None, (
            f"faltou a netpol pipeline-egress-to-{worker} — pipeline não alcança o worker"
        )
        spec = egress_np["spec"]
        assert spec["podSelector"]["matchLabels"]["app"] == "deile-pipeline"
        assert spec["policyTypes"] == ["Egress"]
        rule = spec["egress"][0]
        assert rule["to"] == [{"podSelector": {"matchLabels": {"app": worker}}}]
        adapter = cli_adapters.ADAPTERS[kind]
        assert rule["ports"][0]["port"] == adapter.default_port


# ===== build-cli-workers — Dockerfile.cli-worker + build-arg WORKER_KIND ======


class TestBuildCliWorkers:
    def test_dockerfile_cli_worker_exists_and_uses_build_arg(self):
        df = _REPO / "infra" / "k8s" / "Dockerfile.cli-worker"
        assert df.is_file()
        text = df.read_text(encoding="utf-8")
        assert "ARG WORKER_KIND" in text
        # Cada kind registrado tem um bloco de install gated por WORKER_KIND.
        for kind in _FLEET_KINDS:
            assert f'WORKER_KIND" = "{kind}"' in text, (
                f"Dockerfile.cli-worker sem bloco de install para {kind!r}"
            )

    def test_build_cmd_targets_per_kind_image(self):
        kind = _FLEET_KINDS[0]
        cmd = deploy._cli_worker_build_cmd(kind)
        # cmd pode ser None se nenhum runtime de container existir no host de CI.
        if cmd is None:
            pytest.skip("nenhum runtime de container disponível")
        assert "--build-arg" in cmd
        assert f"WORKER_KIND={kind}" in cmd
        assert f"deile-cli-worker-{kind}:local" in cmd
        assert "Dockerfile.cli-worker" in " ".join(cmd)

    def test_build_cli_workers_dry_run_lists_all_kinds(self, capsys):
        rc = deploy.k8s_build_cli_workers({"dry_run": True, "yes": True, "extra": []})
        assert rc == 0
        out = capsys.readouterr().out
        for kind in _FLEET_KINDS:
            assert f"deile-cli-worker-{kind}:local" in out

    def test_build_cli_workers_rejects_unknown_kind(self, capsys):
        rc = deploy.k8s_build_cli_workers(
            {"dry_run": True, "yes": True, "extra": ["--kind", "nope"]}
        )
        assert rc == 64


# ===== gen-worker verb — dry-run + escrita ====================================


class TestGenWorkerVerb:
    def test_gen_worker_dry_run_prints_yaml(self, capsys):
        kind = _FLEET_KINDS[0]
        rc = deploy.k8s_gen_worker({"dry_run": True, "yes": True, "extra": [kind]})
        assert rc == 0
        out = capsys.readouterr().out
        assert f"{kind}-worker" in out
        assert "NetworkPolicy" in out

    def test_gen_worker_requires_kind(self, capsys):
        rc = deploy.k8s_gen_worker({"dry_run": False, "yes": True, "extra": []})
        assert rc == 64

    def test_gen_worker_rejects_unknown_kind(self, capsys):
        rc = deploy.k8s_gen_worker(
            {"dry_run": False, "yes": True, "extra": ["totally-unknown"]}
        )
        assert rc == 64


# ===== Painel — model-ids do CLI worker derivados do registro =================


class TestPanelCliWorkerModels:
    @pytest.mark.parametrize("kind", _FLEET_KINDS)
    def test_model_picker_uses_adapter_catalog_for_cli_worker(self, kind):
        from _panel import DispatchMatrixView  # noqa: PLC0415

        view = DispatchMatrixView(data=None)
        worker = f"{kind}-worker"
        opts = view._model_picker_options(worker=worker)
        # Primeira opção é a sentinela de clear; o resto vem do adapter.
        assert opts[0] == DispatchMatrixView._CLEAR_SENTINEL_MODEL
        adapter = cli_adapters.ADAPTERS[kind]
        catalog_ids = {m.id for m in adapter.list_models()}
        # Ao menos um model-id do catálogo do adapter aparece no picker.
        assert catalog_ids & set(opts[1:]), (
            f"picker de {worker} não derivou modelos do registro de adapters"
        )

    def test_cli_worker_model_ids_empty_for_core_workers(self):
        from _panel import DispatchMatrixView  # noqa: PLC0415

        assert DispatchMatrixView._cli_worker_model_ids("deile-worker") == []
        assert DispatchMatrixView._cli_worker_model_ids("claude-worker") == []

    def test_deile_worker_picker_uses_provider_model_catalog(self):
        from _panel import DispatchMatrixView  # noqa: PLC0415

        view = DispatchMatrixView(data=None)
        opts = view._model_picker_options(worker="deile-worker")
        # Catálogo provider:model (deile-worker), não model-ids nativos de CLI.
        assert any(":" in o for o in opts[1:])


# ===== provider-env por worker (convenção DEILE_CLI_<KIND>_ENV_<VAR>) ==========


def _qwen_env_doc(rendered: str) -> dict:
    """Extrai a lista ``env`` do container do Deployment renderizado."""
    docs = [d for d in yaml.safe_load_all(rendered) if d]
    dep = next(d for d in docs if d["kind"] == "Deployment")
    return {
        e["name"]: e
        for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
    }


class TestProviderEnvOverride:
    """``DEILE_CLI_<KIND>_ENV_<VAR>`` injeta env de provider por worker.

    Não-sensível → valor literal no Deployment; sensível (``_API_KEY``/
    ``_TOKEN`` ou ``auth_env_keys``) → ``secretKeyRef``, nunca literal. Ausência
    da convenção → manifest inalterado (comportamento atual).
    """

    def test_no_provider_env_leaves_manifest_unchanged(self, monkeypatch):
        monkeypatch.setattr(gen, "read_env_sources", lambda: {})
        rendered = gen.render_manifests("qwen")
        # YAML válido e sem nenhuma var de provider injetada.
        docs = [d for d in yaml.safe_load_all(rendered) if d]
        assert len(docs) >= 5
        env = _qwen_env_doc(rendered)
        assert "OPENAI_BASE_URL" not in env
        assert "OPENAI_MODEL" not in env
        # Comentário no-op presente no bloco.
        assert "sem provider-env DEILE_CLI_QWEN_ENV_" in rendered

    def test_non_sensitive_var_becomes_literal_env(self, monkeypatch):
        monkeypatch.setattr(
            gen, "read_env_sources",
            lambda: {
                "DEILE_CLI_QWEN_ENV_OPENAI_BASE_URL":
                    "https://openrouter.ai/api/v1",
                "DEILE_CLI_QWEN_ENV_OPENAI_MODEL": "qwen/qwen3-coder",
            },
        )
        rendered = gen.render_manifests("qwen")
        env = _qwen_env_doc(rendered)
        assert env["OPENAI_BASE_URL"]["value"] == "https://openrouter.ai/api/v1"
        assert env["OPENAI_MODEL"]["value"] == "qwen/qwen3-coder"
        # Não-sensível não vira secretKeyRef.
        assert "valueFrom" not in env["OPENAI_BASE_URL"]
        assert "valueFrom" not in env["OPENAI_MODEL"]

    def test_sensitive_var_becomes_secret_ref_not_literal(self, monkeypatch):
        secret_value = "sk-super-secret-do-not-leak"
        monkeypatch.setattr(
            gen, "read_env_sources",
            lambda: {
                "DEILE_CLI_QWEN_ENV_OPENAI_API_KEY": secret_value,
                "DEILE_CLI_QWEN_ENV_OPENROUTER_TOKEN": secret_value,
            },
        )
        rendered = gen.render_manifests("qwen")
        # O valor sensível NUNCA aparece materializado no manifest.
        assert secret_value not in rendered
        env = _qwen_env_doc(rendered)
        # OPENROUTER_TOKEN (sufixo _TOKEN) → secretKeyRef no cli-worker-keys.
        ref = env["OPENROUTER_TOKEN"]["valueFrom"]["secretKeyRef"]
        assert ref["name"] == "cli-worker-keys"
        assert ref["key"] == "OPENROUTER_TOKEN"
        # OPENAI_API_KEY já sai pelo AUTH_ENV_BLOCK (auth_env_key do qwen) —
        # presente exatamente uma vez (sem duplicata pelo provider-env).
        docs = [d for d in yaml.safe_load_all(rendered) if d]
        dep = next(d for d in docs if d["kind"] == "Deployment")
        all_names = [
            e["name"]
            for e in dep["spec"]["template"]["spec"]["containers"][0]["env"]
        ]
        assert all_names.count("OPENAI_API_KEY") == 1

    def test_parse_provider_env_is_pure(self):
        source = {
            "DEILE_CLI_QWEN_ENV_OPENAI_BASE_URL": "https://x/v1",
            "DEILE_CLI_QWEN_ENV_": "ignored-empty-varname",
            "DEILE_CLI_AIDER_ENV_OPENAI_MODEL": "other-worker",
            "UNRELATED": "nope",
        }
        parsed = gen.parse_provider_env("qwen", source)
        assert parsed == {"OPENAI_BASE_URL": "https://x/v1"}

    def test_split_provider_env_partitions_by_sensitivity(self):
        adapter = cli_adapters.ADAPTERS["qwen"]
        provider_env = {
            "OPENAI_BASE_URL": "https://x/v1",
            "OPENAI_MODEL": "qwen/qwen3-coder",
            "OPENAI_API_KEY": "sk-x",
            "SOME_TOKEN": "tok",
        }
        literals, secrets = gen.split_provider_env(provider_env, adapter)
        assert literals == {
            "OPENAI_BASE_URL": "https://x/v1",
            "OPENAI_MODEL": "qwen/qwen3-coder",
        }
        assert secrets == {"OPENAI_API_KEY": "sk-x", "SOME_TOKEN": "tok"}

    def test_install_merges_sensitive_provider_keys_into_secret(self, monkeypatch):
        import _cli_worker_install as inst  # noqa: PLC0415

        secret_value = "sk-provider-secret"
        applied_literals: list = []

        def fake_apply(values, *, namespace):
            applied_literals.append(values)
            return True

        monkeypatch.setattr(inst, "_read_env_file", lambda: {})
        # auth_env_key plain ausente; só a var sensível da convenção existe.
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.setattr(
            gen, "read_env_sources",
            lambda: {"DEILE_CLI_QWEN_ENV_OPENAI_API_KEY": secret_value},
        )
        resolved = inst._resolve_auth_keys(
            cli_adapters.ADAPTERS["qwen"], kind="qwen",
        )
        assert resolved.get("OPENAI_API_KEY") == secret_value


# ===== install_cli_worker — on-demand (padrão claude-login generalizado) ======


class _FakeCompleted:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestInstallCliWorker:
    """``install_cli_worker`` orquestra Secret + bearer + manifest + scale.

    kubectl é mockado (nenhuma chamada real ao cluster). Verifica a SEQUÊNCIA
    de comandos, não o efeito no cluster.
    """

    def test_install_sequence_for_env_worker(self, monkeypatch):
        import _cli_worker_install as inst  # noqa: PLC0415

        kind = _FLEET_KINDS[0]
        calls: list = []

        def fake_run(cmd, *a, **kw):
            calls.append(cmd)
            joined = " ".join(cmd)
            # worker-bearer get retorna um token base64 ("tok").
            if "get" in cmd and "worker-bearer" in joined and "jsonpath" in joined:
                return _FakeCompleted(0, stdout="dG9r")  # base64('tok')
            # get cli-worker-keys (merge) → ausente.
            if "get" in cmd and "cli-worker-keys" in joined:
                return _FakeCompleted(1, stdout="")
            return _FakeCompleted(0, stdout="ok")

        # Garante que a chave de API resolve (para popular o Secret).
        adapter = cli_adapters.ADAPTERS[kind]
        for k in adapter.auth_env_keys:
            monkeypatch.setenv(k, "secret-value")
        monkeypatch.setattr(inst, "_read_env_file", lambda: {})
        monkeypatch.setattr(inst.subprocess, "run", fake_run)

        res = inst.install_cli_worker(kind, namespace="deile")
        assert res.ok, res.error
        assert res.keys_secret_applied
        assert res.bearer_applied
        assert res.manifest_applied
        assert res.scaled
        # Houve um scale do worker correto.
        scale_calls = [c for c in calls if "scale" in c]
        assert any(f"deployment/{kind}-worker" in " ".join(c) for c in scale_calls)
        # O Secret do bearer foi para <kind>-worker-bearer.
        assert any(f"{kind}-worker-bearer" in " ".join(c) for c in calls)

    def test_install_unknown_kind_returns_error(self, monkeypatch):
        import _cli_worker_install as inst  # noqa: PLC0415

        res = inst.install_cli_worker("nope-cli", namespace="deile")
        assert res.ok is False
        assert res.error

    def test_install_reports_missing_keys(self, monkeypatch):
        import _cli_worker_install as inst  # noqa: PLC0415

        kind = _FLEET_KINDS[0]
        adapter = cli_adapters.ADAPTERS[kind]
        for k in adapter.auth_env_keys:
            monkeypatch.delenv(k, raising=False)
        monkeypatch.setattr(inst, "_read_env_file", lambda: {})
        monkeypatch.setattr(
            inst.subprocess, "run",
            lambda cmd, *a, **kw: _FakeCompleted(0, stdout="dG9r"),
        )
        res = inst.install_cli_worker(kind, namespace="deile")
        # Sem chave, o Secret é vazio mas o install segue (worker not-ready).
        assert res.missing_keys == list(adapter.auth_env_keys)

    def test_uninstall_deletes_worker_resources(self, monkeypatch):
        import _cli_worker_install as inst  # noqa: PLC0415

        kind = _FLEET_KINDS[0]
        deleted: list = []

        def fake_run(cmd, *a, **kw):
            if "delete" in cmd:
                deleted.append(cmd)
            return _FakeCompleted(0, stdout="deleted")

        monkeypatch.setattr(inst.subprocess, "run", fake_run)
        res = inst.uninstall_cli_worker(kind, namespace="deile")
        assert res.ok
        joined = [" ".join(c) for c in deleted]
        assert any(f"deployment {kind}-worker" in j for j in joined)
        assert any(f"networkpolicy {kind}-worker-netpol" in j for j in joined)
        # NÃO deleta o Secret compartilhado cli-worker-keys.
        assert not any("cli-worker-keys" in j for j in joined)
