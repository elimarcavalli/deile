"""Regressão da MonitorView do painel (`[M]` no deploy.py panel).

Bug (01/jun): entrar na tela `[M]` estourava ``AttributeError: 'int' object
has no attribute 'replace'``. O state do monitor grava ``last_tick`` como
CONTADOR (int) + ``last_tick_epoch`` (epoch), mas o painel tratava
``last_tick`` como timestamp ISO (``_parse_iso(st.last_tick)`` e
``st.last_tick[:19]``). Estes testes reproduzem o render com ``last_tick``
inteiro e garantem que não estoura.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
from rich.console import Console

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"


@pytest.fixture
def pm():
    if str(_INFRA_K8S) not in sys.path:
        sys.path.insert(0, str(_INFRA_K8S))
    spec = importlib.util.spec_from_file_location(
        "panel_monitor_test",
        str(_INFRA_K8S / "_panel_monitor.py"),
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    sys.modules["panel_monitor_test"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _render(renderable) -> str:
    console = Console(file=None, width=120)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


def _snapshot_with_int_last_tick(pm):
    return pm.MonitorSnapshot(
        state=pm.MonitorStateData(
            last_tick=49,  # CONTADOR (int) — era tratado como ISO
            last_tick_epoch=1780344283.0,
            notifications_this_hour=1,
        ),
    )


def test_pod_header_renders_with_int_last_tick(pm):
    view = pm.MonitorView()
    snap = _snapshot_with_int_last_tick(pm)
    out = _render(view._render_pod_header(snap))  # continha _parse_iso(last_tick)
    assert out  # renderizou sem AttributeError


def test_last_tick_panel_renders_with_int(pm):
    view = pm.MonitorView()
    snap = _snapshot_with_int_last_tick(pm)
    out = _render(view._render_last_tick(snap))  # continha last_tick[:19]
    assert "#49" in out  # mostra o contador


def test_footer_visible_when_body_overflows(pm, monkeypatch):
    """O rodapé de atalhos fica PINADO via Layout — não some quando o corpo
    (ex.: audit log grande) passa da altura do terminal (Live screen=True)."""
    import types

    import _panel
    from rich.panel import Panel
    from rich.text import Text

    # head depende de muitos atributos do app real; aqui só nos importa o
    # footer pinado, então simplificamos o head.
    monkeypatch.setattr(_panel, "_head_panel", lambda title, app: Panel(Text("HEAD")))

    snap = pm.MonitorSnapshot(
        pod=pm.MonitorPodInfo(
            found=True,
            name="deile-monitor-x",
            status="Running",
            ready=True,
            age_s=120,
        ),
        state=pm.MonitorStateData(last_tick=51, last_tick_epoch=1780344283.0),
        audit_tail=[
            f"2026-06-01T20:00:{i:02d}Z linha de audit {i}" for i in range(40)
        ],  # corpo alto → transborda
    )
    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: snap, invalidate=lambda: None
        )
    )

    layout = view._render_safe(object())
    # Renderiza num terminal BAIXO (24 linhas) — o corpo não cabe, mas o
    # footer pinado tem de aparecer mesmo assim.
    console = Console(width=140, height=24)
    with console.capture() as cap:
        console.print(layout)
    out = cap.get()
    assert "[i]interval" in out or "interval" in out


# ---------------------------------------------------------------------------
# Testes das correções de bugs (adicionados em 01/jun/2026)
# ---------------------------------------------------------------------------


def test_force_tick_touches_flag_not_rm(pm, monkeypatch):
    """[t] força-tick cria o flag /state/force-tick (não pkill, não rm)."""
    import types

    captured = []

    def fake_exec(self, args, timeout=8.0):
        captured.append(list(args))
        return True, ""

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(pm.MonitorView, "_exec", fake_exec)
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_force_tick(object())

    # Garante que `touch /state/force-tick` foi chamado.
    flat = [arg for args in captured for arg in args]
    assert "touch" in flat, "touch não foi chamado; args capturados: " + str(captured)
    assert (
        "/state/force-tick" in flat
    ), "/state/force-tick não apareceu nos args; args: " + str(captured)
    # O mecanismo antigo (pkill -x sleep) não deve mais ser usado.
    assert not any(
        "pkill" in arg for arg in flat
    ), "pkill ainda é chamado (mecanismo antigo); args: " + str(captured)
    # Garante que rm NÃO foi chamado (não é destrutivo) e que o state não some.
    assert not any(
        "rm" in arg for arg in flat
    ), "rm foi chamado inesperadamente; args: " + str(captured)
    assert not any("monitor-state.json" in arg for arg in flat), (
        "monitor-state.json apareceu nos args (state não deve ser apagado); args: "
        + str(captured)
    )
    assert view._last_ok is True
    assert "force-tick" in view._last_msg.lower() or "forçado" in view._last_msg.lower()


def test_force_tick_failure_surfaces_error(pm, monkeypatch):
    """[t] touch falhando (ex.: /state read-only) vira erro visível, não sucesso."""
    import types

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(
        pm.MonitorView,
        "_exec",
        lambda self, args, timeout=8.0: (
            False,
            "touch: cannot touch '/state/force-tick'",
        ),
    )
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_force_tick(object())

    assert view._last_ok is False
    assert "falha" in view._last_msg.lower()


def test_apply_pause_writes_paused_until(pm, monkeypatch):
    """_apply_pause('30m') cria kill-switch E grava paused_until no state JSON."""
    import types

    captured = []

    def fake_exec(self, args, timeout=8.0):
        captured.append(list(args))
        return True, ""

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(pm.MonitorView, "_exec", fake_exec)
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_pause("30m", object())

    # touch foi chamado (kill-switch).
    assert any(
        "touch" in arg for args in captured for arg in args
    ), "touch não foi chamado; args: " + str(captured)
    # python3 -c foi chamado com paused_until no script.
    python_calls = [args for args in captured if "python3" in args]
    assert python_calls, "python3 não foi chamado para gravar paused_until"
    script_args = " ".join(python_calls[0])
    assert "paused_until" in script_args, (
        "paused_until não está no script python3; script: " + script_args
    )
    # Mensagem inclui "pausado" e a duração.
    assert "30m" in view._last_msg or "pausado" in view._last_msg.lower()
    assert view._last_ok is True


def test_apply_pause_invalid_duration(pm, monkeypatch):
    """_apply_pause com duração inválida seta _last_ok=False e não chama exec."""
    import types

    captured = []

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(
        pm.MonitorView,
        "_exec",
        lambda self, args, timeout=8.0: captured.append(args) or (True, ""),
    )
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_pause("xyz", object())

    assert view._last_ok is False
    assert "inválida" in view._last_msg.lower() or "inválido" in view._last_msg.lower()
    # exec não deve ter sido chamado com touch (kill-switch não ativado para input inválido).
    assert not any(
        "touch" in str(args) for args in captured
    ), "touch foi chamado com duração inválida; args: " + str(captured)


def test_parse_duration_s(pm):
    """_parse_duration_s converte formatos corretamente."""
    parse = pm.MonitorView._parse_duration_s
    assert parse("30m") == 1800
    assert parse("2h") == 7200
    assert parse("90s") == 90
    assert parse("120") == 120
    assert parse("1H") == 3600  # case-insensitive
    assert parse("xyz") is None
    assert parse("") is None
    assert parse("30x") is None


def test_apply_user_id_rejects_non_numeric(pm, monkeypatch):
    """[u] rejeita user ID não-numérico sem chamar kubectl."""
    import types

    exec_called = []

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(
        pm.MonitorView,
        "_exec",
        lambda self, args, timeout=8.0: exec_called.append(args) or (True, ""),
    )
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_user_id("not-a-number", object())

    assert view._last_ok is False
    assert "inválido" in view._last_msg.lower()
    # kubectl não foi chamado.
    assert not exec_called, "kubectl exec foi chamado com UID inválido"


def test_apply_user_id_accepts_numeric(pm, monkeypatch):
    """[u] aceita user ID numérico e chama kubectl com printf."""
    import types

    captured = []

    view = pm.MonitorView(
        monitor_provider=types.SimpleNamespace(
            get=lambda: pm.MonitorSnapshot(),
            invalidate=lambda: None,
        )
    )
    monkeypatch.setattr(
        pm.MonitorView,
        "_exec",
        lambda self, args, timeout=8.0: captured.append(list(args)) or (True, ""),
    )
    monkeypatch.setattr(pm.MonitorView, "_ns", lambda self: "deile")

    view._apply_user_id("123456789012345678", object())

    assert view._last_ok is True
    # printf %s deve estar no comando, não echo.
    flat = " ".join(str(a) for args in captured for a in args)
    assert "printf" in flat, "printf não está nos args: " + flat
    assert "echo" not in flat, "echo (inseguro) presente nos args: " + flat


def test_models_fallback_slugs_valid(pm):
    """Todos os slugs em _MODELS_FALLBACK seguem o padrão provider:model."""
    import re

    slug_re = re.compile(r"^[a-z]+:[a-z0-9._-]+$")
    for slug in pm._MODELS_FALLBACK:
        assert slug_re.match(slug), (
            f"Slug inválido em _MODELS_FALLBACK: {slug!r} "
            "(esperado ^[a-z]+:[a-z0-9._-]+$)"
        )


def test_models_fallback_no_stale_slugs(pm):
    """_MODELS_FALLBACK não contém slugs antigos removidos do YAML."""
    stale = {"openai:gpt-4", "deepseek:deepseek-chat", "google:gemini-2.5-pro"}
    present = set(pm._MODELS_FALLBACK)
    overlap = stale & present
    assert not overlap, f"Slugs desatualizados ainda em _MODELS_FALLBACK: {overlap}"


# ---------------------------------------------------------------------------
# Testes AC1/AC2/AC3 — parser schema novo + compatibilidade schema antigo (#440)
# ---------------------------------------------------------------------------


def test_parse_vigias_new_schema_action(pm):
    """monitor.action com kind=oauth_renew → V1 has_action=True (AC1)."""
    lines = [
        "2026-06-01T10:00:00Z monitor.action kind=oauth_renew V1 ok=true",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert vigias[1].has_action is True, "V1 deveria ter has_action=True"
    assert vigias[1].has_warn is False


def test_parse_vigias_new_schema_ok_false(pm):
    """monitor.vigia.check ok=false → has_warn=True (AC1)."""
    lines = [
        "2026-06-01T10:01:00Z monitor.vigia.check V2 ok=false",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert vigias[2].has_warn is True, "V2 deveria ter has_warn=True por ok=false"


def test_parse_vigias_new_schema_vigia_skip(pm):
    """monitor.vigia.skip → has_warn=True mesmo sem ok=false (AC1)."""
    lines = [
        "2026-06-01T10:02:00Z monitor.vigia.skip V3 reason=no_anomalies",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert vigias[3].has_warn is True, "V3 deveria ter has_warn=True por vigia.skip"


def test_parse_vigias_new_schema_kind_fallback(pm):
    """monitor.action kind=delete_pod sem V<n> explícito → V2 via _KIND_TO_VIGIA (AC1)."""
    lines = [
        "2026-06-01T10:03:00Z monitor.action kind=delete_pod ok=true",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert (
        vigias[2].has_action is True
    ), "V2 deveria ter has_action=True via kind=delete_pod"


def test_parse_vigias_old_schema_fallback(pm):
    """Schema antigo: ACTION + V1 → has_action=True (AC2)."""
    lines = [
        "2026-06-01T09:00:00Z ACTION abc123 V1 renovando oauth",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert (
        vigias[1].has_action is True
    ), "V1 deveria ter has_action=True (schema antigo)"


def test_parse_vigias_malformed_line(pm):
    """Linha malformada não levanta exceção; todos os vigias ficam sem dados (AC3)."""
    lines = [
        "isso nao eh uma linha valida do audit log !!!",
    ]
    vigias = pm.MonitorDataProvider._parse_vigias(None, lines)
    # Não deve levantar exceção; vigias com last_seen_ts=None
    assert all(v.last_seen_ts is None for v in vigias)


def test_parse_vigias_last_seen_ts(pm):
    """last_seen_ts é preenchido corretamente a partir do timestamp ISO (AC3)."""
    lines = [
        "2026-06-01T10:05:00Z monitor.action kind=oauth_renew V1 ok=true",
    ]
    vigias = {v.number: v for v in pm.MonitorDataProvider._parse_vigias(None, lines)}
    assert vigias[1].last_seen_ts is not None, "last_seen_ts deveria ser preenchido"
    assert vigias[1].last_seen_ts.year == 2026
