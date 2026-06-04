"""Testes do initContainer ``bootstrap-creds`` idempotente do claude-worker.

O initContainer (definido em ``infra/k8s/manifests/50-claude-worker-deployment.yaml``)
foi modificado para preservar o ``credentials.json`` no PVC entre restarts —
copia do Secret apenas quando o PVC está ausente OU o Secret tem token mais
recente. Antes, sobrescrevia incondicionalmente, derrubando o refresh in-pod
feito pelo próprio ``claude -p`` a cada rollout.

Como o initContainer roda um shell script (não Python importável), o teste
EXECUTA o script real extraído do YAML em um diretório temporário, exercitando
os quatro cenários canônicos:

  - ``pvc-absent`` — primeira vez no pod, init copia do Secret.
  - ``pvc-newer`` — refresh in-pod funcionou; init preserva.
  - ``secret-newer`` — operador rodou ``claude-login`` recentemente; init copia.
  - ``pvc-malformed`` — corrompido por crash mid-write; init copia (fail-safe).

O script é parseado a partir do YAML pra garantir que estamos testando a
mesma lógica em produção (não uma cópia divergente).
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
_MANIFEST = _REPO / "infra" / "k8s" / "manifests" / "50-claude-worker-deployment.yaml"


def _extract_bootstrap_script() -> str:
    """Lê o bloco ``args: - | <script>`` do initContainer no manifest 50.

    Robusto contra reformatação leve do YAML; usa marcadores únicos
    (``initContainers``, ``bootstrap-creds``, ``args:`` seguido de ``- |``).
    """
    text = _MANIFEST.read_text(encoding="utf-8")
    # Encontra o bloco do initContainer bootstrap-creds.
    bootstrap_anchor = text.find("name: bootstrap-creds")
    assert bootstrap_anchor != -1, "marcador `name: bootstrap-creds` não achado"
    sub = text[bootstrap_anchor:]
    # Próximo ``args:`` é o do initContainer.
    args_anchor = sub.find("args:")
    assert args_anchor != -1, "bloco `args:` não achado dentro do init"
    after = sub[args_anchor:]
    # Captura o conteúdo até a primeira linha cujo indent é MENOR que o do
    # próprio bloco (fim do literal YAML ``- |``). O bloco ``args: - |``
    # tem indent maior que o ``volumeMounts:`` que segue.
    m = re.search(r"args:\s*\n(\s*-\s*\|\s*\n)", after)
    assert m, "regex não casou o início do bloco args:- |"
    block_start = m.end()
    rest = after[block_start:]
    # Detecta o indent comum da primeira linha não-vazia.
    first_line = next(line for line in rest.splitlines() if line.strip())
    block_indent = len(first_line) - len(first_line.lstrip(" "))
    block_lines = []
    for line in rest.splitlines():
        if not line.strip():
            block_lines.append("")
            continue
        line_indent = len(line) - len(line.lstrip(" "))
        if line_indent < block_indent:
            break
        block_lines.append(line[block_indent:])
    return "\n".join(block_lines)


@pytest.fixture
def script_text() -> str:
    return _extract_bootstrap_script()


@pytest.fixture
def fake_pod(tmp_path: Path):
    """Cria a estrutura ``/home/claude/.claude`` + ``/run/secrets/claude`` em tmp.

    O script real referencia caminhos absolutos. Usamos uma adaptação:
    re-write os caminhos para a árvore tmp via env-var, e o script funciona
    inalterado com os mesmos opcodes. Devolve ``(pvc_dir, secret_dir, run)``.
    """
    pvc = tmp_path / "home" / "claude" / ".claude"
    secret = tmp_path / "run" / "secrets" / "claude"
    pvc.mkdir(parents=True, exist_ok=True)
    secret.mkdir(parents=True, exist_ok=True)

    def run(text: str) -> subprocess.CompletedProcess:
        # Substitui paths absolutos pelos do tmp.
        patched = (
            text
            .replace("/home/claude/.claude/credentials.json",
                     str(pvc / "credentials.json"))
            .replace("/run/secrets/claude/credentials.json",
                     str(secret / "credentials.json"))
            .replace("mkdir -p /home/claude/.claude",
                     f"mkdir -p {pvc}")
        )
        return subprocess.run(
            ["bash", "-c", patched], capture_output=True, text=True,
            check=False, timeout=20,
        )

    return pvc, secret, run


def _write_creds(path: Path, expires_at_ms: int, token: str) -> None:
    path.write_text(json.dumps(
        {"claudeAiOauth": {"accessToken": token, "expiresAt": expires_at_ms}}
    ), encoding="utf-8")


class TestInitContainerIdempotency:
    @pytest.mark.unit
    def test_pvc_absent_copies_from_secret(self, script_text, fake_pod):
        """Primeira instalação: PVC sem credentials → init copia do Secret."""
        pvc_dir, secret_dir, run = fake_pod
        _write_creds(secret_dir / "credentials.json", 1_000_000, "from-secret")
        # Garante PVC vazio.
        pvc_file = pvc_dir / "credentials.json"
        if pvc_file.exists():
            pvc_file.unlink()

        result = run(script_text)
        assert result.returncode == 0, f"script falhou: {result.stderr}"
        assert pvc_file.exists()
        loaded = json.loads(pvc_file.read_text())
        assert loaded["claudeAiOauth"]["accessToken"] == "from-secret"
        assert "PVC sem credentials.json" in result.stdout

    @pytest.mark.unit
    def test_pvc_newer_is_preserved(self, script_text, fake_pod):
        """Refresh in-pod ativo: PVC tem token mais recente que Secret → preserva."""
        pvc_dir, secret_dir, run = fake_pod
        _write_creds(secret_dir / "credentials.json", 1_000_000, "from-secret")
        _write_creds(pvc_dir / "credentials.json", 5_000_000, "from-pvc-fresh")

        result = run(script_text)
        assert result.returncode == 0, f"script falhou: {result.stderr}"
        loaded = json.loads((pvc_dir / "credentials.json").read_text())
        assert loaded["claudeAiOauth"]["accessToken"] == "from-pvc-fresh"
        assert "preservado" in result.stdout

    @pytest.mark.unit
    def test_secret_newer_overrides_pvc(self, script_text, fake_pod):
        """Operador rodou claude-login: Secret > PVC → init copia Secret."""
        pvc_dir, secret_dir, run = fake_pod
        _write_creds(secret_dir / "credentials.json", 9_000_000, "from-secret-new")
        _write_creds(pvc_dir / "credentials.json", 5_000_000, "from-pvc-old")

        result = run(script_text)
        assert result.returncode == 0, f"script falhou: {result.stderr}"
        loaded = json.loads((pvc_dir / "credentials.json").read_text())
        assert loaded["claudeAiOauth"]["accessToken"] == "from-secret-new"
        assert "Secret mais recente" in result.stdout

    @pytest.mark.unit
    def test_pvc_malformed_falls_back_to_secret(self, script_text, fake_pod):
        """PVC corrompido (JSON inválido) → init copia Secret (fail-safe).

        O ``read_exp`` retorna ``0`` em parse error; ``Secret >= 0`` causa copy.
        """
        pvc_dir, secret_dir, run = fake_pod
        _write_creds(secret_dir / "credentials.json", 1_000_000, "from-secret")
        (pvc_dir / "credentials.json").write_text("not-json{{", encoding="utf-8")

        result = run(script_text)
        assert result.returncode == 0, f"script falhou: {result.stderr}"
        loaded = json.loads((pvc_dir / "credentials.json").read_text())
        assert loaded["claudeAiOauth"]["accessToken"] == "from-secret"

    @pytest.mark.unit
    def test_equal_expiry_preserves_pvc(self, script_text, fake_pod):
        """Tie-break: PVC == Secret → preserva (PVC). Garantia de não copiar
        inutilmente quando os tokens são idênticos (caso comum logo após boot)."""
        pvc_dir, secret_dir, run = fake_pod
        _write_creds(secret_dir / "credentials.json", 7_777_777, "from-secret")
        _write_creds(pvc_dir / "credentials.json", 7_777_777, "from-pvc-same")

        result = run(script_text)
        assert result.returncode == 0, f"script falhou: {result.stderr}"
        loaded = json.loads((pvc_dir / "credentials.json").read_text())
        # PVC preservado mesmo com expiry igual (>= no script).
        assert loaded["claudeAiOauth"]["accessToken"] == "from-pvc-same"
        assert "preservado" in result.stdout


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
