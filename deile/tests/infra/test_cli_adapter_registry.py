"""Testes do registro auto-descoberto de CLI adapters (Fase 2 — framework).

Cobre:
  1. Contrato/dataclasses do ``cli_adapters.base`` (WorkResult/ResumeCtx/
     ModelInfo/OAuthSpec + Protocol runtime-checkable).
  2. Auto-discovery: dropar um módulo adapter no pacote → ``reload_adapters``
     o registra; ``base`` e módulos ``_privados`` nunca entram; ``get_adapter``
     resolve/levanta.

O pacote ``cli_adapters`` vive em ``infra/k8s/`` (fora do pacote ``deile``); o
path é inserido manualmente — mesma convenção dos demais testes de infra.
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

# Insere infra/k8s no sys.path para importar cli_adapters.
_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402
from cli_adapters import base  # noqa: E402


# --------------------------------------------------------------------------- #
# Contrato / dataclasses
# --------------------------------------------------------------------------- #


def test_workresult_defaults():
    wr = base.WorkResult(ok=True)
    assert wr.ok is True
    assert wr.result_text == ""
    assert wr.error_code is None
    assert wr.cost_usd is None


def test_modelinfo_as_dict_fills_label_from_id():
    mi = base.ModelInfo(id="openrouter/deepseek/deepseek-chat", provider="openrouter")
    d = mi.as_dict()
    assert d["id"] == "openrouter/deepseek/deepseek-chat"
    assert d["label"] == "openrouter/deepseek/deepseek-chat"  # fallback p/ id
    assert d["provider"] == "openrouter"
    assert d["context"] is None and d["notes"] is None
    # Campos de preço/auth são opcionais e default None (retrocompat).
    assert d["price_in"] is None and d["price_out"] is None
    assert d["cached_in"] is None and d["auth"] is None


def test_modelinfo_price_and_auth_roundtrip():
    """Frente 3: ModelInfo carrega preço + auth e serializa em as_dict."""
    mi = base.ModelInfo(
        id="gpt-5-codex", provider="openai",
        price_in=1.25, cached_in=0.125, price_out=10.00, auth="chatgpt",
    )
    d = mi.as_dict()
    assert d["price_in"] == 1.25
    assert d["cached_in"] == 0.125
    assert d["price_out"] == 10.00
    assert d["auth"] == "chatgpt"


def test_oauthspec_fields():
    spec = base.OAuthSpec(
        cred_path="~/.codex/auth.json",
        login_cmd=["codex", "login", "--device-auth"],
        secret_name="codex-credentials",
        renewable=True,
    )
    assert spec.cred_path == "~/.codex/auth.json"
    assert spec.login_cmd[0] == "codex"
    assert spec.renewable is True


def test_base_adapter_satisfies_protocol_with_overrides():
    class _Fake(base.BaseCliAdapter):
        def build_argv(self, **_kw):
            return ["fake"]

        def parse_output(self, *, stdout, stderr, rc):
            return base.WorkResult(ok=rc == 0)

    a = _Fake(kind="fake", default_port=8799)
    assert isinstance(a, base.CliAdapter)
    assert a.build_argv(brief_path="b", model=None, reasoning=None,
                        workdir="w", resume=None) == ["fake"]
    assert a.parse_output(stdout="", stderr="", rc=0).ok is True
    assert a.list_models() == []
    assert a.env_overlay(home="/home/fake") == {}


def test_incomplete_object_does_not_satisfy_protocol():
    class _Missing:
        kind = "x"
        # falta o resto do contrato

    assert not isinstance(_Missing(), base.CliAdapter)


# --------------------------------------------------------------------------- #
# Auto-discovery
# --------------------------------------------------------------------------- #


@pytest.fixture
def synthetic_adapter(tmp_path, monkeypatch):
    """Dropa um módulo adapter sintético no pacote ``cli_adapters`` e recarrega.

    Escreve ``cli_adapters/zzz_fake_<n>.py`` no diretório real do pacote (e o
    remove no teardown), depois chama ``reload_adapters``. Garante isolamento:
    o arquivo é nomeado de forma única e apagado.
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_synthetic_fake.py"
    mod_path.write_text(textwrap.dedent('''\
        from cli_adapters.base import BaseCliAdapter, WorkResult, ModelInfo


        class SyntheticAdapter(BaseCliAdapter):
            def build_argv(self, **kw):
                return ["synthetic", kw["workdir"]]

            def parse_output(self, *, stdout, stderr, rc):
                return WorkResult(ok=(rc == 0), result_text=stdout[:80])

            def list_models(self):
                return [ModelInfo(id="synthetic/model-1", provider="synthetic")]


        ADAPTER = SyntheticAdapter(kind="synthetic", default_port=8799)
    '''), encoding="utf-8")
    try:
        cli_adapters.reload_adapters()
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        # Limpa o módulo importado e restaura o registro pristino.
        sys.modules.pop("cli_adapters.zzz_synthetic_fake", None)
        cli_adapters.reload_adapters()


def test_discovery_registers_synthetic_adapter(synthetic_adapter):
    assert "synthetic" in cli_adapters.ADAPTERS
    adapter = cli_adapters.get_adapter("synthetic")
    assert adapter.kind == "synthetic"
    assert adapter.default_port == 8799
    assert adapter.list_models()[0].id == "synthetic/model-1"


def test_get_adapter_unknown_raises_with_known_list(synthetic_adapter):
    with pytest.raises(KeyError) as exc:
        cli_adapters.get_adapter("does-not-exist")
    assert "synthetic" in str(exc.value)


def test_base_module_is_never_registered():
    # 'base' é o contrato, não um adapter — nunca aparece como kind.
    assert "base" not in cli_adapters.ADAPTERS


def test_reload_mutates_in_place_preserving_reference():
    ref = cli_adapters.ADAPTERS
    returned = cli_adapters.reload_adapters()
    assert returned is ref  # mesmo objeto (mutação in-place, não rebind)
