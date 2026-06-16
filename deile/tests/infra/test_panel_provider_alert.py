"""Alerta VERMELHO de crédito/provedor por worker no painel (issue #445).

Cobre a cadeia inteira no lado do painel (o produtor do sinal é o backend do
worker, testado à parte):

* ``ProviderHealthProvider._scan_pod`` classifica o corte por provedor a partir
  do log (reutilizando ``_worker_core.classify_provider_error`` — fonte única).
* ``_render_grouped_pods`` pinta o grupo/linha de VERMELHO (crédito esgotado)
  ou AMARELO (rate-limit/5xx) com selo claro na tela [1] Pods.
* ``_provider_alert_banner`` produz o banner global VERMELHO/AMARELO visível em
  qualquer tela — e SOME quando a frota fica saudável.
* ``_alerts_from_data`` injeta os alertas no feed (crit p/ crédito, warn p/
  transitório) e casa padrão crú (402/credit) via o code do core.
* Saldo proativo OpenRouter: indicador VERMELHO quando baixo/zerado.

Nenhum teste toca rede ou cluster — tudo via monkeypatch do ``_capture_text``
/ providers mockados.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from rich.console import Console
from rich.table import Table

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _row(
    name: str, role: str, *, code: Optional[str] = None, **extra
) -> "panel.PodRow":
    return panel.PodRow(
        icon=extra.get("icon", "●"),
        name=name,
        role=role,
        status=extra.get("status", "Running"),
        age=extra.get("age", "5m"),
        restarts=extra.get("restarts", "0"),
        last_activity=extra.get("last_activity", "—"),
        doing_now=extra.get("doing_now", "idle"),
        busy=extra.get("busy", False),
        provider_error_code=code,
    )


def _render_grouped(rows) -> str:
    tbl = Table(expand=True)
    for col in ("icon", "pod", "status", "age", "r", "last", "doing"):
        tbl.add_column(col)
    panel._render_grouped_pods(
        tbl,
        rows,
        panel._restart_text,
        panel._doing_now_render,
    )
    console = Console(width=200)
    with console.capture() as cap:
        console.print(tbl)
    return cap.get()


def _render(renderable) -> str:
    console = Console(width=200)
    with console.capture() as cap:
        console.print(renderable)
    return cap.get()


class _FakeProvider:
    """Imita o contrato ``.get()`` dos providers do painel."""

    def __init__(self, value):
        self._value = value
        self.last_error = None

    def get(self, force: bool = False):
        return self._value

    def peek(self):
        return self._value


class _FakeData:
    """PanelData mínimo para os helpers de alerta (sem cluster real)."""

    def __init__(
        self,
        *,
        provider_health=None,
        openrouter=None,
        pods=None,
        workers=None,
        pipeline=None,
        github=None,
    ):
        self.provider_health = (
            _FakeProvider(provider_health) if provider_health is not None else None
        )
        self.openrouter_balance = (
            _FakeProvider(openrouter) if openrouter is not None else None
        )
        self.pods = _FakeProvider(pods or [])
        self.workers = _FakeProvider(workers or {})
        self.pipeline = _FakeProvider(pipeline)
        self.github = _FakeProvider(github)

    def errors(self):
        return []


# --------------------------------------------------------------------------- #
# ProviderHealthProvider — classificação a partir do log
# --------------------------------------------------------------------------- #


def test_scan_pod_classifies_insufficient_credit(monkeypatch):
    log = (
        "2026-06-08T10:00:00.000000Z dispatch.received task=abc\n"
        "2026-06-08T10:01:00.000000Z ERROR provider returned 402 Payment Required: "
        "insufficient credit, add more credits\n"
    )
    monkeypatch.setattr(pd, "_capture_text", lambda cmd, timeout=5.0: log)
    prov = pd.ProviderHealthProvider(enabled=True)
    prov._kubectl = "/usr/bin/kubectl"
    err = prov._scan_pod("opencode-worker-xyz", "opencode-worker")
    assert err is not None
    assert err.code == "INSUFFICIENT_CREDIT"
    assert err.severity == "crit"
    assert err.label == "CRÉDITO ESGOTADO"
    assert err.detected_ts is not None


def test_scan_pod_classifies_rate_limit_as_warn(monkeypatch):
    log = "2026-06-08T10:00:00.000000Z HTTP 429 too many requests, rate limit\n"
    monkeypatch.setattr(pd, "_capture_text", lambda cmd, timeout=5.0: log)
    prov = pd.ProviderHealthProvider(enabled=True)
    prov._kubectl = "/usr/bin/kubectl"
    err = prov._scan_pod("qwen-worker-1", "qwen-worker")
    assert err is not None
    assert err.code == "RATE_LIMIT"
    assert err.severity == "warn"


def test_scan_pod_matches_raw_402_pattern(monkeypatch):
    # Code estruturado ausente — apenas o número crú 402 no texto.
    log = "2026-06-08T10:00:00.000000Z openrouter: status 402\n"
    monkeypatch.setattr(pd, "_capture_text", lambda cmd, timeout=5.0: log)
    prov = pd.ProviderHealthProvider(enabled=True)
    prov._kubectl = "/usr/bin/kubectl"
    err = prov._scan_pod("aider-worker-1", "aider-worker")
    assert err is not None and err.code == "INSUFFICIENT_CREDIT"


def test_scan_pod_healthy_returns_none(monkeypatch):
    log = (
        "2026-06-08T10:00:00.000000Z dispatch.received task=abc\n"
        "2026-06-08T10:01:00.000000Z dispatch.completed task=abc ok=true\n"
    )
    monkeypatch.setattr(pd, "_capture_text", lambda cmd, timeout=5.0: log)
    prov = pd.ProviderHealthProvider(enabled=True)
    prov._kubectl = "/usr/bin/kubectl"
    assert prov._scan_pod("deile-worker-1", "worker") is None


def test_scan_pod_picks_most_recent_cut(monkeypatch):
    # Um 429 antigo seguido de um 402 mais novo → o mais novo (402/crédito) ganha.
    log = (
        "2026-06-08T10:00:00.000000Z HTTP 429 rate limit\n"
        "2026-06-08T10:05:00.000000Z provider 402 insufficient credit\n"
    )
    monkeypatch.setattr(pd, "_capture_text", lambda cmd, timeout=5.0: log)
    prov = pd.ProviderHealthProvider(enabled=True)
    prov._kubectl = "/usr/bin/kubectl"
    err = prov._scan_pod("opencode-worker-1", "opencode-worker")
    assert err is not None and err.code == "INSUFFICIENT_CREDIT"


def test_provider_health_disabled_is_noop():
    prov = pd.ProviderHealthProvider(enabled=False)
    assert prov.get() == {}  # fallback do Cache, sem kubectl


# --------------------------------------------------------------------------- #
# [1] Pods — grupo/linha VERMELHO + selo
# --------------------------------------------------------------------------- #


def test_grouped_pods_red_on_credit():
    rows = [
        _row("opencode-worker-a", "opencode-worker", code="INSUFFICIENT_CREDIT"),
    ]
    out = _render_grouped(rows)
    assert "CRÉDITO ESGOTADO" in out
    assert "⛔" in out


def test_grouped_pods_yellow_on_rate_limit():
    rows = [_row("qwen-worker-a", "qwen-worker", code="RATE_LIMIT")]
    out = _render_grouped(rows)
    assert "RATE LIMIT" in out
    assert "PROVIDER DEGRADADO" in out  # cabeçalho amarelo


def test_grouped_pods_no_badge_when_healthy():
    rows = [_row("deile-worker-a", "worker")]
    out = _render_grouped(rows)
    assert "CRÉDITO ESGOTADO" not in out
    assert "RATE LIMIT" not in out


def test_grouped_pods_credit_wins_over_rate_limit_in_group():
    # Dois pods do mesmo tipo: um rate-limit, um crédito → cabeçalho VERMELHO.
    rows = [
        _row("opencode-worker-a", "opencode-worker", code="RATE_LIMIT"),
        _row("opencode-worker-b", "opencode-worker", code="INSUFFICIENT_CREDIT"),
    ]
    out = _render_grouped(rows)
    assert "CRÉDITO ESGOTADO" in out  # selo do grupo é o pior caso


# --------------------------------------------------------------------------- #
# Banner global
# --------------------------------------------------------------------------- #


def test_banner_red_for_credit():
    err = pd.WorkerProviderError(
        pod_name="opencode-worker-a",
        role="opencode-worker",
        code="INSUFFICIENT_CREDIT",
    )
    data = _FakeData(provider_health={"opencode-worker-a": err})
    banner = panel._provider_alert_banner(data)
    assert banner is not None
    out = _render(banner)
    assert "CORTE POR PROVEDOR LLM" in out
    assert "opencode-worker" in out
    assert "INSUFFICIENT_CREDIT" in out


def test_banner_yellow_for_rate_limit_only():
    err = pd.WorkerProviderError(
        pod_name="qwen-worker-a",
        role="qwen-worker",
        code="RATE_LIMIT",
    )
    data = _FakeData(provider_health={"qwen-worker-a": err})
    banner = panel._provider_alert_banner(data)
    assert banner is not None
    out = _render(banner)
    assert "DEGRADAÇÃO DE PROVEDOR" in out
    assert "RATE_LIMIT" in out


def test_banner_absent_when_healthy():
    data = _FakeData(provider_health={})
    assert panel._provider_alert_banner(data) is None


def test_banner_absent_when_provider_missing():
    # local-only / demo: provider_health is None → sem banner.
    data = _FakeData()
    assert panel._provider_alert_banner(data) is None


def test_banner_resolves_after_error_clears():
    err = pd.WorkerProviderError(
        pod_name="opencode-worker-a",
        role="opencode-worker",
        code="INSUFFICIENT_CREDIT",
    )
    data_err = _FakeData(provider_health={"opencode-worker-a": err})
    assert panel._provider_alert_banner(data_err) is not None
    # Recarga de crédito → próxima leitura vem vazia → banner some.
    data_ok = _FakeData(provider_health={})
    assert panel._provider_alert_banner(data_ok) is None


# --------------------------------------------------------------------------- #
# Feed de ALERTS
# --------------------------------------------------------------------------- #


def test_alerts_include_credit_as_crit():
    err = pd.WorkerProviderError(
        pod_name="opencode-worker-a",
        role="opencode-worker",
        code="INSUFFICIENT_CREDIT",
    )
    data = _FakeData(
        provider_health={"opencode-worker-a": err},
        pods=[],
        workers={},
        pipeline=type("PS", (), {"last_action_age_s": None})(),
        github=type("GH", (), {"issues": []})(),
    )
    alerts = panel._alerts_from_data(data)
    crit = [a for a in alerts if a.severity == "crit"]
    assert any("CRÉDITO ESGOTADO" in a.msg for a in crit)


def test_alerts_rate_limit_is_warn():
    err = pd.WorkerProviderError(
        pod_name="qwen-worker-a",
        role="qwen-worker",
        code="RATE_LIMIT",
    )
    data = _FakeData(
        provider_health={"qwen-worker-a": err},
        pipeline=type("PS", (), {"last_action_age_s": None})(),
        github=type("GH", (), {"issues": []})(),
    )
    alerts = panel._alerts_from_data(data)
    warns = [a for a in alerts if "RATE LIMIT" in a.msg]
    assert warns and all(a.severity == "warn" for a in warns)


# --------------------------------------------------------------------------- #
# Saldo proativo OpenRouter
# --------------------------------------------------------------------------- #


def test_openrouter_balance_severity():
    assert (
        pd.OpenRouterBalance(
            available=True,
            total_credits=10.0,
            total_usage=9.5,
        ).severity
        == "crit"
    )  # $0.50 restante
    assert (
        pd.OpenRouterBalance(
            available=True,
            total_credits=10.0,
            total_usage=7.0,
        ).severity
        == "warn"
    )  # $3.00 restante (≤ 2× limiar)
    assert (
        pd.OpenRouterBalance(
            available=True,
            total_credits=10.0,
            total_usage=1.0,
        ).severity
        is None
    )  # $9.00 restante — saudável
    assert pd.OpenRouterBalance(available=False).severity is None


def test_openrouter_alert_red_when_low():
    bal = pd.OpenRouterBalance(
        available=True,
        total_credits=10.0,
        total_usage=9.9,
    )
    data = _FakeData(openrouter=bal)
    alert = panel._openrouter_alert(data)
    assert alert is not None and alert.severity == "crit"
    assert "OpenRouter" in alert.msg


def test_openrouter_alert_none_when_healthy():
    bal = pd.OpenRouterBalance(
        available=True,
        total_credits=10.0,
        total_usage=0.0,
    )
    assert panel._openrouter_alert(_FakeData(openrouter=bal)) is None


def test_openrouter_alert_none_when_unavailable():
    bal = pd.OpenRouterBalance(available=False)
    assert panel._openrouter_alert(_FakeData(openrouter=bal)) is None


def test_openrouter_balance_no_key_is_unavailable(monkeypatch, tmp_path):
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    # Aponta o resolver para um .env inexistente.
    monkeypatch.setattr(
        pd,
        "_read_openrouter_key",
        lambda: None,
    )
    prov = pd.OpenRouterBalanceProvider()
    bal = prov._fetch()
    assert bal.available is False


def test_banner_includes_openrouter_when_zero():
    bal = pd.OpenRouterBalance(
        available=True,
        total_credits=5.0,
        total_usage=5.0,
    )
    data = _FakeData(provider_health={}, openrouter=bal)
    banner = panel._provider_alert_banner(data)
    assert banner is not None
    assert "OpenRouter" in _render(banner)
