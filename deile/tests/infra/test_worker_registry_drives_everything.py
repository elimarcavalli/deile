"""Teste de regressão de ESCALABILIDADE da frota multi-CLI (plano §1.0).

**Invariante de 1ª classe:** adicionar um worker CLI novo deve ser trivial — um
adapter em ``cli_adapters/<kind>.py`` o torna automaticamente um dispatcher
válido, com endpoint derivado e visível no painel, **sem editar nenhum
consumidor**. A fonte única de verdade é o registro ``cli_adapters.ADAPTERS``.

Este teste falha se alguém RE-HARDCODAR uma lista de workers em qualquer
consumidor (``dispatch_resolver.VALID_DISPATCHERS`` / a lista do painel / os
endpoints) em vez de derivá-la do registro. Cobre duas frentes:

1. **Derivação dinâmica (comportamento):** registra um adapter SINTÉTICO em
   runtime, recarrega o registro e prova que o resolver e o painel passam a
   enxergar o novo worker — sem nenhuma edição de código. Se a derivação fosse
   hardcoded, o worker sintético NÃO apareceria e o teste falharia.
2. **Anti-hardcode (estático):** varre o fonte dos consumidores e falha se um
   literal de lista-de-workers reaparecer onde deveria haver derivação.

O pacote ``cli_adapters`` vive em ``infra/k8s/`` — path inserido manualmente
(convenção dos testes de infra; ver ``test_cli_worker_server.py``).
"""

from __future__ import annotations

import sys
import textwrap
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import cli_adapters  # noqa: E402

from deile.orchestration.pipeline import dispatch_resolver as dr  # noqa: E402

_SYNTH_KIND = "zzz_synthworker"
_SYNTH_DISPATCHER = f"{_SYNTH_KIND}-worker"
_SYNTH_PORT = 8795


@pytest.fixture
def synthetic_adapter(monkeypatch):
    """Dropa um adapter sintético no pacote ``cli_adapters`` e recarrega.

    O adapter declara ``kind`` + ``default_port`` distintos dos workers núcleo,
    para provar que o resolver e o painel o descobrem por derivação. Limpa o
    módulo + recarrega o registro no teardown para não vazar entre testes.
    """
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / f"{_SYNTH_KIND}.py"
    mod_path.write_text(
        textwrap.dedent(
            f'''\
            from cli_adapters.base import BaseCliAdapter, ModelInfo, WorkResult


            class SynthAdapter(BaseCliAdapter):
                def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                    return ["true"]

                def parse_output(self, *, stdout, stderr, rc):
                    return WorkResult(ok=(rc == 0))

                def list_models(self):
                    return [ModelInfo(id="synth/model-x", provider="synth")]


            ADAPTER = SynthAdapter(kind="{_SYNTH_KIND}", default_port={_SYNTH_PORT})
            '''
        ),
        encoding="utf-8",
    )
    cli_adapters.reload_adapters()
    try:
        yield
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop(f"cli_adapters.{_SYNTH_KIND}", None)
        cli_adapters.reload_adapters()


# ===== Frente 1 — derivação dinâmica (o resolver enxerga o adapter novo) =====


def test_core_workers_always_present():
    """Os dois workers núcleo existem mesmo com o registro CLI vazio."""
    valid = dr.get_valid_dispatchers()
    assert "deile-worker" in valid
    assert "claude-worker" in valid


def test_registering_adapter_adds_dispatcher(synthetic_adapter):
    """Registrar um adapter torna ``<kind>-worker`` um dispatcher válido.

    Núcleo set-difference prova que o crescimento veio do registro, não de um
    literal: se ``get_valid_dispatchers`` fosse hardcoded, o worker sintético
    estaria ausente.
    """
    valid = dr.get_valid_dispatchers()
    assert _SYNTH_DISPATCHER in valid, (
        f"{_SYNTH_DISPATCHER!r} ausente — get_valid_dispatchers não derivou "
        "do registro de adapters (provável re-hardcode)"
    )
    # O worker sintético vive na frota CLI (set-difference do núcleo), provando
    # que o crescimento veio do registro e não de um literal. Não se assume que
    # a frota CLI esteja vazia — adapters reais (ex.: opencode) também aparecem.
    fleet = valid - dr.BUILTIN_DISPATCHERS
    assert _SYNTH_DISPATCHER in fleet
    # Núcleo intacto: nenhum builtin sumiu da derivação.
    assert dr.BUILTIN_DISPATCHERS <= valid


def test_registered_dispatcher_validates(synthetic_adapter):
    """``is_valid_dispatcher`` aceita a forma canônica E a curta do worker novo."""
    assert dr.is_valid_dispatcher(_SYNTH_DISPATCHER) is True
    assert dr.is_valid_dispatcher(_SYNTH_KIND) is True  # forma curta <kind>
    assert dr.is_valid_dispatcher(_SYNTH_DISPATCHER.upper()) is True  # case-insensitive


def test_endpoint_derived_from_adapter_port(synthetic_adapter, monkeypatch):
    """O endpoint do worker novo é DERIVADO do ``default_port`` do adapter.

    Nenhum literal de URL/porta para CLI workers no resolver — a porta vem do
    metadado do adapter.
    """
    monkeypatch.delenv(
        f"DEILE_{_SYNTH_KIND.upper()}_WORKER_ENDPOINT", raising=False
    )
    assert (
        dr.get_endpoint_for(_SYNTH_DISPATCHER)
        == f"http://{_SYNTH_DISPATCHER}:{_SYNTH_PORT}"
    )


def test_endpoint_env_override_for_cli_worker(synthetic_adapter, monkeypatch):
    """A env var ``DEILE_<KIND>_WORKER_ENDPOINT`` sobrescreve o default derivado."""
    monkeypatch.setenv(
        f"DEILE_{_SYNTH_KIND.upper()}_WORKER_ENDPOINT", "http://localhost:19999"
    )
    assert dr.get_endpoint_for(_SYNTH_DISPATCHER) == "http://localhost:19999"


def test_resolve_stage_dispatcher_accepts_cli_worker(synthetic_adapter, monkeypatch):
    """Um stage pode ser apontado ao worker novo via env — sem editar o resolver."""
    for stage in dr.PIPELINE_STAGES:
        monkeypatch.delenv(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}", raising=False)
    monkeypatch.delenv("DEILE_PIPELINE_DISPATCH_MODE", raising=False)
    monkeypatch.setenv("DEILE_PIPELINE_DISPATCH_IMPLEMENT", _SYNTH_DISPATCHER)
    assert dr.resolve_stage_dispatcher("implement") == _SYNTH_DISPATCHER


def test_adapter_without_port_is_skipped(monkeypatch):
    """Adapter sem ``default_port`` válido não vira dispatcher (sem endpoint)."""
    pkg_dir = Path(cli_adapters.__path__[0])
    mod_path = pkg_dir / "zzz_noport.py"
    mod_path.write_text(
        textwrap.dedent(
            '''\
            from cli_adapters.base import BaseCliAdapter, WorkResult


            class NoPortAdapter(BaseCliAdapter):
                def build_argv(self, *, brief_path, model, reasoning, workdir, resume, task_id=""):
                    return ["true"]

                def parse_output(self, *, stdout, stderr, rc):
                    return WorkResult(ok=True)


            ADAPTER = NoPortAdapter(kind="zzz_noport")  # default_port = 0
            '''
        ),
        encoding="utf-8",
    )
    cli_adapters.reload_adapters()
    try:
        assert "zzz_noport-worker" not in dr.get_valid_dispatchers()
    finally:
        mod_path.unlink(missing_ok=True)
        sys.modules.pop("cli_adapters.zzz_noport", None)
        cli_adapters.reload_adapters()


def test_unregistering_adapter_removes_dispatcher(synthetic_adapter):
    """Sanity: depois do teardown do fixture o worker some (derivação viva)."""
    # Dentro do fixture, está presente:
    assert _SYNTH_DISPATCHER in dr.get_valid_dispatchers()
    # Remove manualmente e recarrega — simula o teardown.
    pkg_dir = Path(cli_adapters.__path__[0])
    (pkg_dir / f"{_SYNTH_KIND}.py").unlink(missing_ok=True)
    sys.modules.pop(f"cli_adapters.{_SYNTH_KIND}", None)
    cli_adapters.reload_adapters()
    assert _SYNTH_DISPATCHER not in dr.get_valid_dispatchers()


# ===== Frente 1b — o painel deriva a lista de workers do mesmo registro =====


def test_panel_worker_list_includes_registered_adapter(synthetic_adapter):
    """A lista de workers do painel é DERIVADA do registro (não hardcoded)."""
    from _panel import DispatchMatrixView  # noqa: PLC0415

    workers = DispatchMatrixView._canonical_workers()
    assert "deile-worker" in workers
    assert "claude-worker" in workers
    assert _SYNTH_DISPATCHER in workers, (
        "painel não derivou o worker novo do registro — provável re-hardcode "
        "da lista em DispatchMatrixView"
    )


def test_panel_worker_picker_options_include_adapter(synthetic_adapter):
    """O picker per-stage de worker mostra o worker novo + a sentinela."""
    from _panel import DispatchMatrixView  # noqa: PLC0415

    view = DispatchMatrixView(data=None)
    opts = view._worker_picker_options()
    assert _SYNTH_DISPATCHER in opts
    assert any("global" in o.lower() or "default" in o.lower() for o in opts)


# ===== Frente 2 — anti-hardcode estático (o fonte não re-fixou listas) =======


def _read(rel: str) -> str:
    return (_REPO / rel).read_text(encoding="utf-8")


def test_resolver_does_not_hardcode_valid_dispatchers_literal():
    """``VALID_DISPATCHERS`` não pode ser um ``frozenset({...})`` literal.

    Deve ser atribuído a partir de :func:`get_valid_dispatchers` (derivação).
    Um literal com os dois workers escritos à mão é exatamente o anti-padrão
    que este teste barra.
    """
    src = _read("deile/orchestration/pipeline/dispatch_resolver.py")
    # A linha de atribuição final de VALID_DISPATCHERS deve usar a função.
    assert "VALID_DISPATCHERS: FrozenSet[str] = get_valid_dispatchers()" in src, (
        "VALID_DISPATCHERS deve ser derivado de get_valid_dispatchers(), "
        "não de um frozenset literal"
    )
    # E não pode haver um frozenset literal com claude-worker fora do conjunto
    # NÚCLEO declarado (BUILTIN_DISPATCHERS). O único literal permitido é o do
    # BUILTIN_DISPATCHERS (os dois workers núcleo têm server dedicado).
    builtin_literal = 'frozenset({"deile-worker", "claude-worker"})'
    occurrences = src.count(builtin_literal)
    assert occurrences == 1, (
        f"esperado exatamente 1 literal de workers núcleo (BUILTIN_DISPATCHERS), "
        f"achei {occurrences} — uma lista-de-workers extra foi hardcodada"
    )


def test_panel_does_not_hardcode_worker_picker_list():
    """O painel não pode reter ``["...", "deile-worker", "claude-worker"]``."""
    src = _read("infra/k8s/_panel.py")
    forbidden = (
        '["(clear override)", "deile-worker", "claude-worker"]',
        '"deile-worker",\n            "claude-worker",\n        ]',
    )
    for literal in forbidden:
        assert literal not in src, (
            f"lista de workers hardcodada no painel: {literal!r} — derive de "
            "DispatchMatrixView._canonical_workers()"
        )
    # A derivação deve estar presente.
    assert "_canonical_workers" in src
    assert "get_valid_dispatchers" in src
