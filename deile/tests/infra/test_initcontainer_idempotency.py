"""Testes da configuração de auth do claude-worker (issue #603).

Após a migração para ``claude setup-token`` (issue #603), o mecanismo de auth
mudou:
  - ANTES: initContainer ``bootstrap-creds`` copiava ``credentials.json`` do Secret
    para o PVC; o servidor lia o arquivo e exportava ``ANTHROPIC_AUTH_TOKEN``.
  - DEPOIS: ``CLAUDE_CODE_OAUTH_TOKEN`` é injetado como env var no pod a partir
    do Secret K8s (secretKeyRef). Sem initContainer, sem credentials.json no PVC.

Este arquivo testa:
  1. O manifest 50 NÃO tem o initContainer ``bootstrap-creds`` (removido).
  2. O manifest 50 TEM a env var ``CLAUDE_CODE_OAUTH_TOKEN`` via secretKeyRef.
  3. O Secret usa a key ``CLAUDE_CODE_OAUTH_TOKEN`` (não ``credentials.json``).
  4. Os helpers legacy (``_force_clear_pvc_creds``) ainda funcionam para compat.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO / "infra" / "k8s" / "manifests" / "50-claude-worker-deployment.yaml"


class TestManifestAuthSetupToken:
    """Verifica que o manifest 50 usa o modelo de auth setup-token (issue #603)."""

    @pytest.mark.unit
    def test_no_bootstrap_creds_initcontainer(self):
        """O manifest 50 NÃO deve ter o initContainer ``bootstrap-creds``.

        O initContainer foi removido na migração para setup-token. Sua presença
        indicaria que o manifest não foi atualizado e o pod tentaria copiar
        credentials.json de um Secret que não tem mais esse formato.
        """
        text = _MANIFEST.read_text(encoding="utf-8")
        # Verifica que não há definição de initContainers (só comentários podem
        # mencionar bootstrap-creds para documentar a remoção).
        assert "initContainers:" not in text, (
            "initContainers: encontrado no manifest 50 — "
            "deve ter sido removido na migração para setup-token (issue #603)"
        )
        # Verifica que não há entry com name: bootstrap-creds (YAML ativo).
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("- name: bootstrap-creds") or stripped == "name: bootstrap-creds":
                pytest.fail(
                    f"initContainer 'bootstrap-creds' encontrado como YAML ativo: {line!r}\n"
                    "Deve ter sido removido na migração para setup-token (issue #603)"
                )

    @pytest.mark.unit
    def test_has_claude_code_oauth_token_env_var(self):
        """O manifest 50 DEVE ter ``CLAUDE_CODE_OAUTH_TOKEN`` via secretKeyRef."""
        text = _MANIFEST.read_text(encoding="utf-8")
        assert "CLAUDE_CODE_OAUTH_TOKEN" in text, (
            "CLAUDE_CODE_OAUTH_TOKEN não encontrado no manifest 50 — "
            "deve ser injetado como env var via secretKeyRef (issue #603 §3)"
        )
        assert "secretKeyRef" in text, (
            "secretKeyRef não encontrado no manifest 50 — "
            "CLAUDE_CODE_OAUTH_TOKEN deve vir de secretKeyRef no Secret claude-credentials"
        )

    @pytest.mark.unit
    def test_no_credentials_json_volume_mount(self):
        """O manifest 50 NÃO deve montar o Secret claude-credentials como arquivo.

        O Secret ``claude-credentials`` agora tem key ``CLAUDE_CODE_OAUTH_TOKEN``
        injetado via env var (secretKeyRef), não como volumeMount de arquivo.
        Um volumeMount do Secret antigo (key=credentials.json) causaria erro
        porque o Secret não tem mais essa key.
        """
        text = _MANIFEST.read_text(encoding="utf-8")
        # O mount path era /run/secrets/claude (note: NÃO /run/secrets/claude-worker).
        # Verificamos que o Secret claude-credentials não é mais montado como arquivo.
        for line in text.splitlines():
            stripped = line.strip()
            if "mountPath: /run/secrets/claude" in stripped and "claude-worker" not in stripped:
                pytest.fail(
                    f"volumeMount de /run/secrets/claude encontrado no manifest 50: {line!r}\n"
                    "O Secret claude-credentials não deve mais ser montado como arquivo — "
                    "use env var via secretKeyRef (issue #603 §3)"
                )
        # Também verificamos que o volume claude-credentials não aparece como Secret volume.
        import re  # noqa: PLC0415
        if re.search(r"secretName:\s*claude-credentials", text):
            pytest.fail(
                "secretName: claude-credentials encontrado em volumes do manifest 50 — "
                "o Secret deve ser acessado via secretKeyRef (env var), não volumeMount "
                "(issue #603 §3)"
            )

    @pytest.mark.unit
    def test_no_anthropic_auth_token_in_env(self):
        """O manifest 50 NÃO deve setar ``ANTHROPIC_AUTH_TOKEN`` como env var.

        ANTHROPIC_AUTH_TOKEN tem precedência sobre CLAUDE_CODE_OAUTH_TOKEN na
        ordem de auth do claude CLI. Se presente no manifest, mascararia o
        token de assinatura e quebraria a frota (issue #603 §1).
        """
        text = _MANIFEST.read_text(encoding="utf-8")
        # Rejeita qualquer linha que declare ANTHROPIC_AUTH_TOKEN como env var.
        for line in text.splitlines():
            stripped = line.strip()
            if "ANTHROPIC_AUTH_TOKEN" in stripped and (
                stripped.startswith("name:") or stripped.startswith("- name:")
            ):
                pytest.fail(
                    f"ANTHROPIC_AUTH_TOKEN encontrado como env var no manifest 50: {line!r}\n"
                    "Deve ser removido — tem precedência sobre CLAUDE_CODE_OAUTH_TOKEN "
                    "(issue #603 §1)"
                )


class TestForceClearPvcCreds:
    """O fluxo ``claude-login --switch`` invoca ``_force_clear_pvc_creds``
    ANTES do rollout para garantir que o initContainer reaja como
    ``pvc-absent`` e copie a conta nova do Secret."""

    @pytest.mark.unit
    def test_force_clear_helper_exists_and_handles_missing_pod(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """O helper retorna True quando o pod não existe (cluster fresco)."""
        import sys

        sys.path.insert(0, str(_REPO / "infra" / "k8s"))
        try:
            import _claude_install  # noqa: PLC0415
        finally:
            sys.path.pop(0)

        # Simula kubectl exec com "pod not found"
        def fake_run(cmd, **kw):
            class R:
                returncode = 1
                stderr = "Error from server (NotFound): pods not found"
                stdout = ""

            return R()

        monkeypatch.setattr(_claude_install.subprocess, "run", fake_run)
        assert _claude_install._force_clear_pvc_creds(namespace="deile") is True

    @pytest.mark.unit
    def test_force_clear_helper_handles_timeout(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Timeout durante exec é tratado como não-fatal (retry no próximo boot)."""
        import sys

        sys.path.insert(0, str(_REPO / "infra" / "k8s"))
        try:
            import _claude_install  # noqa: PLC0415
        finally:
            sys.path.pop(0)

        def raises(cmd, **kw):
            raise subprocess.TimeoutExpired(cmd, 15)

        monkeypatch.setattr(_claude_install.subprocess, "run", raises)
        assert _claude_install._force_clear_pvc_creds(namespace="deile") is True

    @pytest.mark.unit
    def test_force_clear_helper_succeeds_when_pod_running(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """Exec sucesso (rc=0) é caminho feliz."""
        import sys

        sys.path.insert(0, str(_REPO / "infra" / "k8s"))
        try:
            import _claude_install  # noqa: PLC0415
        finally:
            sys.path.pop(0)

        def fake_run(cmd, **kw):
            class R:
                returncode = 0
                stderr = ""
                stdout = ""

            return R()

        monkeypatch.setattr(_claude_install.subprocess, "run", fake_run)
        assert _claude_install._force_clear_pvc_creds(namespace="deile") is True
