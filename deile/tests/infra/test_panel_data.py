"""Unit tests for the panel data providers + alert engine.

Covers ``infra/k8s/_panel_data.py`` (Cache TTL, parsers, providers) and
the alert engine + activity adapter that live in ``infra/k8s/_panel.py``.
The infra scripts live outside the ``deile`` package, so the path is
inserted on sys.path for the import (same pattern as ``test_worker_resume``).

Os providers de cluster são exercitados com kubectl mockado via
``subprocess.run`` patch — nenhum teste toca o cluster real ou faz call
de rede. O CostsProvider usa um SQLite temporário.
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _panel as panel  # noqa: E402
import _panel_data as pd  # noqa: E402

# ===== Cache TTL ============================================================

class TestCache:
    def test_first_call_invokes_fetcher(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        assert cache.get() == "ok"
        assert calls == [1]

    def test_second_call_never_refetches_via_get(self):
        # Contrato novo: get() NUNCA bloqueia depois do primeiro fetch —
        # devolve sempre o cached. O refresh fica por conta do
        # BackgroundRefresher chamando `maybe_refresh()`.
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()
        cache.get()
        cache.get()
        assert calls == [1]

    def test_get_returns_cached_even_when_stale(self):
        # Mesmo com TTL=0, get() devolve o cache existente. Sem essa
        # garantia, render no thread principal podia disparar fetch
        # sincrono e congelar a UI.
        calls: List[int] = []
        cache = pd.Cache(ttl_s=0.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()                # primeiro fetch (cold start)
        cache.get()                # cache stale, mas NÃO refaz
        cache.get()
        assert calls == [1]

    def test_force_bypasses_ttl(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()
        cache.get(force=True)
        assert calls == [1, 1]

    def test_maybe_refresh_respects_ttl(self):
        # `maybe_refresh` faz fetch só se TTL venceu — é o que o
        # BackgroundRefresher chama em loop.
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()                              # 1º fetch (cold)
        assert cache.maybe_refresh() is False    # TTL ainda fresco
        assert calls == [1]

    def test_maybe_refresh_fetches_when_stale(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=0.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()                              # 1º fetch (cold)
        assert cache.maybe_refresh() is True     # TTL=0 → sempre stale
        assert calls == [1, 1]

    def test_invalidate_marks_stale_without_dropping_value(self):
        # `invalidate` (usado pelo hotkey [r]) marca como vencido mas
        # devolve o valor antigo via get() — o BackgroundRefresher
        # repõe no próximo tick sem segurar a UI.
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: "fresh",
                         fallback="-")
        cache.get()
        cache.invalidate()
        # get() ainda devolve "fresh", sem chamar fetcher de novo.
        assert cache.get() == "fresh"
        # maybe_refresh agora rebusca.
        assert cache.maybe_refresh() is True

    def test_error_keeps_last_value_and_records_error(self):
        attempts = {"n": 0}

        def fetcher() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return "good"
            raise RuntimeError("boom")

        cache = pd.Cache(ttl_s=0.0, fetcher=fetcher, fallback="fallback")
        assert cache.get() == "good"
        # `get()` não refaz fetch (contrato novo); precisamos do
        # `maybe_refresh` para vir o erro.
        assert cache.maybe_refresh() is True
        assert cache.get() == "good"             # mantém último valor bom
        assert cache.last_error is not None
        assert "boom" in cache.last_error

    def test_error_before_any_good_value_returns_fallback(self):
        def boom() -> str:
            raise RuntimeError("never returns")

        cache = pd.Cache(ttl_s=10.0, fetcher=boom, fallback="DEFAULT")
        assert cache.get() == "DEFAULT"


# ===== _fmt_age =============================================================

class TestFmtAge:
    @pytest.mark.parametrize("secs,expected", [
        (None, "—"),
        (-5, "0s"),
        (0, "0s"),
        (45, "45s"),
        (60, "1m"),
        (119, "1m"),
        (3600, "1h"),
        (3660, "1h01m"),
        (86400, "1d"),
        (90000, "1d"),
    ])
    def test_humanizes(self, secs, expected):
        assert pd._fmt_age(secs) == expected


# ===== _parse_k8s_ts / _parse_log_line ======================================

class TestK8sTs:
    def test_parses_rfc3339_z(self):
        ts = pd._parse_k8s_ts("2026-05-23T14:21:56Z")
        assert ts is not None
        assert ts.year == 2026 and ts.tzinfo is not None

    def test_parses_rfc3339_offset(self):
        ts = pd._parse_k8s_ts("2026-05-23T11:22:03.124543858-03:00")
        assert ts is not None

    def test_none_on_garbage(self):
        assert pd._parse_k8s_ts("not a ts") is None
        assert pd._parse_k8s_ts(None) is None


class TestParseLogLine:
    def test_extracts_ts_and_body(self):
        raw = ("2026-05-23T14:21:56.123-03:00 2026-05-23 17:21:56,123 INFO "
               "deile.foo something happened")
        ll = pd._parse_log_line(raw)
        assert ll is not None
        assert ll.ts.year == 2026
        assert "deile.foo something happened" in ll.body

    def test_returns_none_when_no_ts(self):
        assert pd._parse_log_line("no timestamp here") is None


# ===== _classify_pipeline_line =============================================

class TestPipelineClassifier:
    def _line(self, body: str) -> pd.LogLine:
        return pd.LogLine(ts=datetime(2026, 5, 23, 14, 0, tzinfo=timezone.utc),
                          body=body)

    def test_mention_issue(self):
        ev = pd._classify_pipeline_line(self._line(
            "deile.orchestration.pipeline.stages mention group issue:278: "
            "triggers=['assignee']"
        ))
        assert ev is not None
        assert ev.action == "mention"
        assert ev.target == "#278"
        assert "assignee" in ev.detail

    def test_mention_pr(self):
        ev = pd._classify_pipeline_line(self._line(
            "stages mention group pr:291: triggers=['reviewer']"
        ))
        assert ev is not None and ev.target == "PR291"

    def test_dispatch_starting(self):
        ev = pd._classify_pipeline_line(self._line(
            "INFO deile.infrastructure.deile_worker_client worker dispatch starting"
        ))
        assert ev is not None and ev.action == "dispatch"
        assert "starting" in ev.detail

    def test_http_post(self):
        ev = pd._classify_pipeline_line(self._line(
            'httpx HTTP Request: POST http://x/v1/dispatch "HTTP/1.1 200 OK"'
        ))
        assert ev is not None and ev.action == "http"
        assert "200" in ev.detail

    def test_startup(self):
        ev = pd._classify_pipeline_line(self._line(
            "INFO deile.pipeline.runner starting pipeline monitor (repo=...)"
        ))
        assert ev is not None and ev.action == "startup"

    def test_unrelated_returns_none(self):
        assert pd._classify_pipeline_line(self._line(
            "completely unrelated log line about something else"
        )) is None


# ===== _derive_workflow =====================================================

class TestDeriveWorkflow:
    def test_empty(self):
        assert pd._derive_workflow([]) == ""

    def test_picks_workflow_label(self):
        assert pd._derive_workflow(["bug", "~workflow:em_implementacao"]) \
            == "em_implementacao"

    def test_bloqueada_wins_over_other_workflow_label(self):
        # Estado terminal: o pipeline a respeita mesmo quando outra label
        # de fase ainda está presente. Sem essa precedência, a UI mostraria
        # o estado anterior em vez do bloqueio.
        assert pd._derive_workflow([
            "~workflow:em_implementacao", "~workflow:bloqueada",
        ]) == "bloqueada"
        assert pd._derive_workflow([
            "~workflow:bloqueada", "~workflow:em_implementacao",
        ]) == "bloqueada"

    def test_non_workflow_labels_ignored(self):
        assert pd._derive_workflow(["help wanted", "P0"]) == ""


# ===== PodsProvider =========================================================

class TestPodsProvider:
    def _payload(self) -> Dict[str, Any]:
        return {
            "items": [
                {
                    "metadata": {
                        "name": "deile-worker-abc",
                        "labels": {"app": "deile-worker", "role": "deile"},
                    },
                    "status": {
                        "phase": "Running",
                        "startTime": "2026-05-23T14:00:00Z",
                        "containerStatuses": [
                            {"ready": True, "restartCount": 0,
                             "state": {"running": {}}},
                        ],
                    },
                    "spec": {"nodeName": "test-node"},
                },
                {
                    "metadata": {
                        "name": "deilebot-xyz",
                        "labels": {"app": "deilebot"},
                    },
                    "status": {
                        "phase": "Pending",
                        "containerStatuses": [],
                    },
                    "spec": {},
                },
                {
                    "metadata": {
                        "name": "unknown-app",
                        "labels": {"app": "something-else"},
                    },
                    "status": {"phase": "Running",
                               "containerStatuses": [{"ready": True,
                                                       "restartCount": 5}]},
                    "spec": {},
                },
            ],
        }

    def _provider_with_payload(self, payload: Dict[str, Any]) -> pd.PodsProvider:
        # TTL alto: o primeiro `get()` dentro do `with patch` cacheia, e
        # as chamadas subsequentes (no corpo do teste) servem do cache —
        # mesmo se o patch já saiu de escopo.
        prov = pd.PodsProvider(ttl_s=600.0)
        prov._kubectl = "kubectl"
        with patch.object(pd, "_capture_json", return_value=payload):
            prov.get(force=True)
        return prov

    def test_no_kubectl_returns_fallback(self, monkeypatch):
        # `kubectl_bin()` é chamado tanto no __init__ quanto no
        # `_resolve_kubectl` (lazy fallback adicionado no branch
        # a68ace9). Monkeypatch garante que nenhum dos dois encontra.
        monkeypatch.setattr(pd, "kubectl_bin", lambda: None)
        prov = pd.PodsProvider(ttl_s=10.0)
        # Cache fallback é []; nenhum erro propagado pra UI.
        assert prov.get() == []
        assert prov.last_error is not None

    def test_parses_pods_with_roles(self):
        prov = self._provider_with_payload(self._payload())
        rows = prov.get()
        names = [r.name for r in rows]
        assert "deile-worker-abc" in names
        assert "deilebot-xyz" in names
        roles = {r.name: r.role for r in rows}
        assert roles["deile-worker-abc"] == "worker"
        assert roles["deilebot-xyz"] == "bot"
        assert roles["unknown-app"] == "other"

    def test_ordering_pipeline_first_then_worker(self):
        payload = self._payload()
        payload["items"].append({
            "metadata": {"name": "deile-pipeline-zzz",
                         "labels": {"app": "deile-pipeline"}},
            "status": {"phase": "Running",
                       "startTime": "2026-05-23T14:00:00Z",
                       "containerStatuses": [{"ready": True,
                                              "restartCount": 0}]},
            "spec": {},
        })
        prov = self._provider_with_payload(payload)
        rows = prov.get()
        assert rows[0].role == "pipeline"
        assert rows[1].role == "worker"

    def test_aggregates_restart_count_across_containers(self):
        prov = self._provider_with_payload(self._payload())
        rows = {r.name: r for r in prov.get()}
        assert rows["unknown-app"].restarts == 5
        assert rows["deile-worker-abc"].restarts == 0

    def test_kubectl_failure_falls_back(self):
        prov = pd.PodsProvider(ttl_s=0.0)
        prov._kubectl = "kubectl"
        with patch.object(pd, "_capture_json", return_value=None):
            assert prov.get() == []
        assert prov.last_error is not None


# ===== WorkerProvider (busy detection) =====================================

class TestWorkerProvider:
    def _build(self) -> pd.WorkerProvider:
        prov = pd.WorkerProvider(ttl_s=0.0)
        prov._kubectl = "kubectl"
        return prov

    def _recent_log(self, *bodies: str) -> str:
        # Format `kubectl logs --timestamps`: <iso>\t<body>
        # Use timestamps "agora" para o cálculo busy/idle bater.
        now = datetime.now(timezone.utc)
        out = []
        for i, body in enumerate(bodies):
            ts = (now - timedelta(seconds=i * 2))
            out.append(ts.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00")
                       + f" {body}")
        return "\n".join(out)

    def test_idle_when_only_health_in_window(self):
        prov = self._build()
        text = self._recent_log(
            'aiohttp.access "GET /v1/health HTTP/1.1" 200 237',
            'aiohttp.access "GET /v1/health HTTP/1.1" 200 237',
        )
        names_text = "worker-1"

        def fake_capture(cmd, timeout):
            if "jsonpath" in cmd[-1]:
                return names_text
            return text

        with patch.object(pd, "_capture_text", side_effect=fake_capture):
            states = prov.get(force=True)
        assert "worker-1" in states
        assert states["worker-1"].busy is False

    def test_busy_when_dispatch_within_90s(self):
        prov = self._build()
        text = self._recent_log(
            'aiohttp.access "POST /v1/dispatch HTTP/1.1" 200 237',
        )
        names_text = "worker-2"

        def fake_capture(cmd, timeout):
            if "jsonpath" in cmd[-1]:
                return names_text
            return text

        with patch.object(pd, "_capture_text", side_effect=fake_capture):
            states = prov.get(force=True)
        assert states["worker-2"].busy is True

    def test_idle_when_dispatch_older_than_90s(self):
        prov = self._build()
        # Linha com timestamp "antigo" — 5 min atrás
        old_ts = (datetime.now(timezone.utc) - timedelta(minutes=5))
        old_str = (old_ts.strftime("%Y-%m-%dT%H:%M:%S.000000000+00:00")
                   + ' POST /v1/dispatch HTTP/1.1 200')
        names_text = "worker-3"

        def fake_capture(cmd, timeout):
            if "jsonpath" in cmd[-1]:
                return names_text
            return old_str

        with patch.object(pd, "_capture_text", side_effect=fake_capture):
            states = prov.get(force=True)
        assert states["worker-3"].busy is False


# ===== GitHubProvider =======================================================

class TestGitHubProvider:
    def _items(self) -> List[Dict[str, Any]]:
        return [
            {
                "number": 296,
                "title": "[FEATURE] foo",
                "state": "open",
                "labels": [{"name": "feature"},
                           {"name": "~workflow:em_implementacao"}],
                "assignees": [{"login": "deile-one"}],
                "updated_at": "2026-05-23T12:00:00Z",
                "html_url": "https://github.com/x/y/issues/296",
            },
            {
                "number": 283,
                "title": "[FEATURE] bar",
                "state": "open",
                "labels": [{"name": "~workflow:em_implementacao"},
                           {"name": "~workflow:bloqueada"}],
                "assignees": [{"login": "elimarcavalli"}],
                "updated_at": "2026-05-22T20:00:00Z",
                "html_url": "https://github.com/x/y/issues/283",
            },
            {
                "number": 291,
                "title": "PR demo",
                "state": "open",
                "labels": [{"name": "~review:em_andamento"}],
                "assignees": [{"login": "deile-one"}],
                "pull_request": {"url": "..."},
                "updated_at": "2026-05-23T11:36:53Z",
                "html_url": "https://github.com/x/y/pull/291",
            },
        ]

    def test_separates_issues_from_prs(self):
        prov = pd.GitHubProvider(ttl_s=0.0)
        prov._gh = "gh"
        with patch.object(pd, "_capture_json", return_value=self._items()):
            snap = prov.get(force=True)
        assert len(snap.issues) == 2
        assert len(snap.prs) == 1
        assert snap.prs[0].number == 291

    def test_bloqueada_takes_priority(self):
        prov = pd.GitHubProvider(ttl_s=0.0)
        prov._gh = "gh"
        with patch.object(pd, "_capture_json", return_value=self._items()):
            snap = prov.get(force=True)
        by_n = {it.number: it for it in snap.issues}
        assert by_n[283].workflow == "bloqueada"
        assert by_n[283].blocked is True

    def test_state_counts(self):
        prov = pd.GitHubProvider(ttl_s=0.0)
        prov._gh = "gh"
        with patch.object(pd, "_capture_json", return_value=self._items()):
            snap = prov.get(force=True)
        assert snap.issue_states.get("em_implementacao") == 1
        assert snap.issue_states.get("bloqueada") == 1
        assert snap.pr_states.get("em_andamento") == 1

    def test_no_gh_fallback(self):
        prov = pd.GitHubProvider(ttl_s=10.0)
        prov._gh = None
        snap = prov.get()
        assert snap.issues == [] and snap.prs == []


# ===== CostsProvider ========================================================

class TestCostsProvider:
    def _populated_db(self, tmp_path: Path) -> Path:
        db = tmp_path / "usage.db"
        conn = sqlite3.connect(str(db))
        conn.execute("""
            CREATE TABLE usage_records (
              id INTEGER PRIMARY KEY,
              timestamp REAL NOT NULL,
              provider_id TEXT NOT NULL,
              model_id TEXT NOT NULL,
              tier TEXT NOT NULL,
              session_id TEXT NOT NULL,
              prompt_tokens INTEGER, completion_tokens INTEGER,
              cached_tokens INTEGER, total_tokens INTEGER,
              cost_usd REAL, latency_ms INTEGER,
              success INTEGER, error_type TEXT
            )
        """)
        now = time.time()
        rows = [
            (now - 30, "anthropic", "claude", "premium", "s1",
             100, 50, 0, 150, 0.10, 1000, 1, None),
            (now - 60, "anthropic", "claude", "premium", "s1",
             200, 80, 0, 280, 0.15, 1000, 1, None),
            (now - 1800, "openai", "gpt-4", "premium", "s2",
             100, 50, 0, 150, 0.05, 800, 1, None),
            (now - 90000, "deepseek", "ds", "basic", "s3",  # >24h ago: filtrado
             50, 20, 0, 70, 0.99, 500, 1, None),
        ]
        conn.executemany(
            "INSERT INTO usage_records VALUES (NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            rows,
        )
        conn.commit()
        conn.close()
        return db

    def test_missing_db_returns_empty_snapshot(self, tmp_path):
        prov = pd.CostsProvider(db_path=tmp_path / "nope.db", ttl_s=0.0)
        snap = prov.get()
        assert snap.records_24h == 0
        assert snap.total_24h == 0.0
        assert prov.last_error is None  # fallback gracioso, sem erro

    def test_aggregates_within_24h(self, tmp_path):
        db = self._populated_db(tmp_path)
        prov = pd.CostsProvider(db_path=db, ttl_s=0.0)
        snap = prov.get(force=True)
        assert snap.records_24h == 3   # 4ª linha tem >24h, fica fora
        assert snap.total_24h == pytest.approx(0.30, abs=0.001)
        assert snap.by_provider_24h["anthropic"] == pytest.approx(0.25, abs=0.001)
        assert snap.by_provider_24h["openai"] == pytest.approx(0.05, abs=0.001)
        assert "deepseek" not in snap.by_provider_24h

    def test_1h_window_subset(self, tmp_path):
        db = self._populated_db(tmp_path)
        prov = pd.CostsProvider(db_path=db, ttl_s=0.0)
        snap = prov.get(force=True)
        # As 3 linhas dentro de 1h (30s, 60s e 30min): 0.10 + 0.15 + 0.05.
        # A linha de >24h não entra (já validado no test_aggregates_within_24h).
        assert snap.total_1h == pytest.approx(0.30, abs=0.001)

    def test_top_sessions(self, tmp_path):
        db = self._populated_db(tmp_path)
        prov = pd.CostsProvider(db_path=db, ttl_s=0.0)
        snap = prov.get(force=True)
        sids = [s[0] for s in snap.top_sessions_24h]
        assert sids[0] == "s1"     # mais caro


# ===== Alerts engine ========================================================

class TestAlerts:
    def _data_with(self, pods=None, pipeline=None, issues=None,
                   errors=None) -> Any:
        d = MagicMock()
        d.pods.get.return_value = pods or []
        ps = MagicMock()
        if pipeline is not None:
            for k, v in pipeline.items():
                setattr(ps, k, v)
        else:
            ps.last_action_age_s = None
        d.pipeline.get.return_value = ps
        snap = MagicMock()
        snap.issues = issues or []
        d.github.get.return_value = snap
        d.errors.return_value = errors or []
        return d

    def test_no_alerts_when_clean(self):
        d = self._data_with(pods=[], pipeline={"last_action_age_s": 10})
        assert panel._alerts_from_data(d) == []

    def test_pod_restart_crit_above_3(self):
        pod = MagicMock()
        pod.name, pod.restarts, pod.age_s = "x", 4, 3600
        d = self._data_with(pods=[pod],
                            pipeline={"last_action_age_s": 10})
        alerts = panel._alerts_from_data(d)
        assert any(a.severity == "crit" for a in alerts)

    def test_pod_restart_warn_below_30min(self):
        pod = MagicMock()
        pod.name, pod.restarts, pod.age_s = "x", 1, 600
        d = self._data_with(pods=[pod],
                            pipeline={"last_action_age_s": 10})
        alerts = panel._alerts_from_data(d)
        assert any(a.severity == "warn" for a in alerts)

    def test_pipeline_stale_warns(self):
        d = self._data_with(pipeline={"last_action_age_s": 600})
        alerts = panel._alerts_from_data(d)
        assert any("travado" in a.msg.lower() or "sem ação" in a.msg for a in alerts)

    def test_blocked_issues_warn(self):
        it = MagicMock()
        it.number, it.blocked, it.workflow = 283, True, "bloqueada"
        d = self._data_with(pipeline={"last_action_age_s": 10}, issues=[it])
        alerts = panel._alerts_from_data(d)
        assert any("bloqueada" in a.msg for a in alerts)

    def test_demo_mode_returns_synthetic(self):
        # Sem cluster, devolve os mocks de _panel_demo.ALERTS.
        alerts = panel._alerts_from_data(None)
        assert alerts  # não vazio


# ===== _pod_rows adapter ====================================================

class TestModelsProvider:
    def _yaml(self, tmp_path: Path) -> Path:
        path = tmp_path / "model_providers.yaml"
        path.write_text(
            "version: 1\n"
            "models:\n"
            "  - provider_id: anthropic\n"
            "    model_id: claude-opus-4-7\n"
            "    display_name: Claude Opus 4.7\n"
            "    tier: tier_1\n"
            "    label: flagship\n"
            "    pricing:\n"
            "      input_per_1m_usd: 5.0\n"
            "      output_per_1m_usd: 25.0\n"
            "  - provider_id: openai\n"
            "    model_id: gpt-5.4\n"
            "    display_name: GPT-5.4\n"
            "    tier: tier_2\n"
            "    label: balanced\n"
            "    pricing:\n"
            "      input_per_1m_usd: 2.5\n"
            "      output_per_1m_usd: 15.0\n",
            encoding="utf-8",
        )
        return path

    def test_parses_models_from_yaml(self, tmp_path):
        prov = pd.ModelsProvider(yaml_path=self._yaml(tmp_path), ttl_s=0.0)
        models = prov.get(force=True)
        assert len(models) == 2
        slugs = [m.slug for m in models]
        assert "anthropic:claude-opus-4-7" in slugs
        assert "openai:gpt-5.4" in slugs

    def test_pricing_extracted(self, tmp_path):
        prov = pd.ModelsProvider(yaml_path=self._yaml(tmp_path), ttl_s=0.0)
        models = {m.slug: m for m in prov.get(force=True)}
        assert models["anthropic:claude-opus-4-7"].input_cost_per_1m == 5.0
        assert models["openai:gpt-5.4"].output_cost_per_1m == 15.0

    def test_missing_yaml_falls_back_empty(self, tmp_path):
        prov = pd.ModelsProvider(yaml_path=tmp_path / "nope.yaml", ttl_s=0.0)
        assert prov.get() == []
        assert prov.last_error is not None


class TestCurrentModelProvider:
    def test_extracts_from_deployment_json(self):
        sample = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "env": [
                                    {"name": "OTHER", "value": "x"},
                                    {"name": "DEILE_PREFERRED_MODEL",
                                     "value": "anthropic:claude-opus-4-7"},
                                ],
                            },
                        ],
                    },
                },
            },
        }
        assert pd.CurrentModelProvider._extract(sample) \
            == "anthropic:claude-opus-4-7"

    def test_missing_env_returns_none(self):
        sample = {"spec": {"template": {"spec": {
            "containers": [{"env": [{"name": "OTHER", "value": "x"}]}],
        }}}}
        assert pd.CurrentModelProvider._extract(sample) is None

    def test_no_containers_returns_none(self):
        assert pd.CurrentModelProvider._extract(
            {"spec": {"template": {"spec": {"containers": []}}}}
        ) is None

    def test_fetch_uses_kubectl_per_deployment(self):
        prov = pd.CurrentModelProvider(
            deployments=("deile-worker", "deile-pipeline"), ttl_s=0.0,
        )
        prov._kubectl = "kubectl"
        payloads = {
            "deile-worker": {"spec": {"template": {"spec": {"containers": [
                {"env": [{"name": "DEILE_PREFERRED_MODEL",
                          "value": "anthropic:claude-sonnet-4-6"}]},
            ]}}}},
            "deile-pipeline": None,
        }

        def fake_capture(cmd, timeout=None):
            # cmd = [kubectl, -n, deile, get, deployment/<name>, -o, json]
            dep_arg = cmd[4].split("/", 1)[1]
            return payloads[dep_arg]

        with patch.object(pd, "_capture_json", side_effect=fake_capture):
            result = prov.get(force=True)
        assert result["deile-worker"] == "anthropic:claude-sonnet-4-6"
        assert result["deile-pipeline"] is None


class TestSetPreferredModel:
    def test_no_kubectl_returns_false(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: None)
        ok, msg = pd.set_preferred_model("deile-worker",
                                         "anthropic:claude-opus-4-7")
        assert ok is False
        assert "kubectl" in msg

    def test_success_returns_true(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "kubectl")
        mock_proc = MagicMock(returncode=0, stdout="ok\n", stderr="")
        with patch("subprocess.run", return_value=mock_proc):
            ok, _ = pd.set_preferred_model("deile-worker", "x:y")
        assert ok is True

    def test_failure_propagates_stderr(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "kubectl")
        mock_proc = MagicMock(returncode=1, stdout="", stderr="forbidden\n")
        with patch("subprocess.run", return_value=mock_proc):
            ok, msg = pd.set_preferred_model("deile-worker", "x:y")
        assert ok is False
        assert "forbidden" in msg

    @pytest.mark.parametrize("bad_slug", [
        "evil\nDEILE_OTHER=injected",   # newline → outra env via shell
        "x=y",                          # '=' não é permitido (corromperia argv)
        "with space",                   # espaço não permitido
        "ctrl\x01char",                 # caractere de controle
        "ctrl\x00null",                 # NUL
        "",                             # vazio
        "/leading-slash",               # primeiro char inválido
        "a" * 200,                      # comprimento > 128
    ])
    def test_rejects_malicious_slugs_without_calling_kubectl(self, bad_slug,
                                                              monkeypatch):
        """B2: slugs com caracteres perigosos são recusados ANTES de
        chegar ao kubectl (subprocess.run nunca deve ser chamado)."""
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "kubectl")
        with patch("subprocess.run") as run:
            ok, msg = pd.set_preferred_model("deile-worker", bad_slug)
        assert ok is False
        assert "slug" in msg.lower()
        run.assert_not_called()

    def test_accepts_typical_anthropic_slug(self, monkeypatch):
        """Sanity: o slug canônico não pode regredir junto com a validação."""
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "kubectl")
        mock_proc = MagicMock(returncode=0, stdout="ok\n", stderr="")
        with patch("subprocess.run", return_value=mock_proc):
            ok, _ = pd.set_preferred_model(
                "deile-worker", "anthropic:claude-opus-4-7",
            )
        assert ok is True


class TestPodRowsAdapter:
    def test_demo_mode_uses_demo_pods(self):
        rows = panel._pod_rows(None)
        assert rows
        assert any(r.role == "worker" for r in rows)

    def test_with_data_passes_through(self):
        d = MagicMock()
        pod = MagicMock()
        pod.name, pod.role, pod.status, pod.ready, pod.restarts = (
            "p1", "worker", "Running", True, 0,
        )
        pod.age_s, pod.started_at, pod.node = 3600, None, "n1"
        d.pods.get.return_value = [pod]
        ws = MagicMock()
        ws.busy = True
        ws.last_activity_s = 5
        ws.last_substantive_body = "some text"
        d.workers.get.return_value = {"p1": ws}
        ps = MagicMock()
        ps.last_action_age_s = None
        ps.last_action_summary = ""
        d.pipeline.get.return_value = ps
        rows = panel._pod_rows(d)
        assert rows[0].busy is True
        assert rows[0].icon == "⚡"

    def test_cluster_up_but_no_pods_returns_empty(self):
        """n4: cobrir o caminho 'cluster respondeu, mas namespace está
        vazio' — antes da gente assumir, pods.get() pode devolver lista
        vazia (namespace recém-criado, scale 0 etc) sem erro."""
        d = MagicMock()
        d.pods.get.return_value = []
        d.workers.get.return_value = {}
        ps = MagicMock()
        ps.last_action_age_s = None
        ps.last_action_summary = ""
        d.pipeline.get.return_value = ps
        rows = panel._pod_rows(d)
        assert rows == []


# ===== Universal mode (k8s + local) =========================================
#
# Tests cobrem a Fase "painel universal" (issue de evolução da PR
# #294 — `--namespace`, processos locais, tail de logs locais, audit
# local). Nenhum mock de dados visíveis ao usuário; só fixtures de
# arquivos tmp + monkeypatch de `ps`/`kubectl`.

class TestRuntimeContext:
    def test_detect_defaults(self):
        ctx = pd.RuntimeContext.detect()
        assert ctx.namespace == "deile"
        assert ctx.pipeline_deploy == "deile-pipeline"
        assert ctx.worker_deploy == "deile-worker"
        assert ctx.bot_deploy == "deilebot"
        assert ctx.repo  # detectado do origin OU fallback "elimarcavalli/deile"

    def test_detect_with_overrides(self):
        ctx = pd.RuntimeContext.detect(
            namespace="my-ns",
            pipeline_deploy="p1",
            worker_deploy="w1",
            repo="org/repo",
        )
        assert ctx.namespace == "my-ns"
        assert ctx.pipeline_deploy == "p1"
        assert ctx.worker_deploy == "w1"
        assert ctx.repo == "org/repo"

    def test_demo_disables_modes(self):
        ctx = pd.RuntimeContext(demo=True)
        assert ctx.k8s_available is False
        assert ctx.local_available is False
        assert ctx.mode_label == "demo (mocks)"

    def test_k8s_force_blocks_local(self, monkeypatch):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/k")
        ctx = pd.RuntimeContext(k8s_force=True)
        assert ctx.k8s_available is True
        assert ctx.local_available is False

    def test_local_force_blocks_k8s(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/k")
        # logs_dir existe → local_available=True
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(local_force=True, logs_dir=logs)
        assert ctx.k8s_available is False
        assert ctx.local_available is True

    def test_mode_label_hybrid(self, monkeypatch, tmp_path):
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/k")
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(logs_dir=logs,
                                usage_db=tmp_path / "no.db")
        assert ctx.mode_label == "k8s + local"


class TestLocalProcessesProvider:
    """Mocka `ps` via patch de `subprocess.run` para validar parsing."""

    def _ps_output(self, lines: List[str]) -> str:
        return "\n".join(lines) + "\n"

    def test_classify_local_process(self):
        assert pd._classify_local_process("python3 deile.py") == "local-deile"
        assert pd._classify_local_process(
            "/usr/bin/python -m deilebot run --provider discord"
        ) == "local-bot"
        assert pd._classify_local_process(
            "python -m deile.orchestration.pipeline.monitor"
        ) == "local-pipeline"
        assert pd._classify_local_process("/usr/bin/python -m other") is None
        # Generic fallback: any python+deile mention.
        assert pd._classify_local_process(
            "python /opt/something/deile-helper.py"
        ) == "local-other"

    def test_parse_etime(self):
        assert pd._parse_etime("01:23") == 83
        assert pd._parse_etime("12:34:56") == 12 * 3600 + 34 * 60 + 56
        assert pd._parse_etime("2-03:04:05") == 2 * 86400 + 3 * 3600 + 4 * 60 + 5
        assert pd._parse_etime("garbage") == 0

    def test_fetch_parses_and_filters_panel_pid(self, monkeypatch):
        provider = pd.LocalProcessesProvider()
        # Inclui o próprio PID para garantir que é filtrado.
        import os as _os
        mine = _os.getpid()
        fake_ps = (
            f"  {mine}    0.0  6144  00:01 python infra/k8s/deploy.py panel\n"
            "   123    1.5  4096  10:00 python3 deile.py\n"
            "   456    0.0  2048  01:23:45 python -m deilebot run\n"
            "   789    0.0  1024  03-12:00:00 python -m deile.orchestration.x\n"
        )
        mock_proc = MagicMock(returncode=0, stdout=fake_ps, stderr="")
        monkeypatch.setattr(pd.shutil, "which", lambda b: "/bin/ps" if b == "ps" else None)
        with patch("subprocess.run", return_value=mock_proc):
            procs = provider.get()
        # 3 esperados (123, 456, 789) — o próprio painel filtrado.
        assert len(procs) == 3
        pids = [p.pid for p in procs]
        assert mine not in pids
        # Ordem: deile > pipeline > bot
        assert procs[0].role == "local-deile"
        assert procs[0].pid == 123
        assert procs[1].role == "local-pipeline"
        assert procs[2].role == "local-bot"

    def test_fetch_no_ps_raises_into_fallback(self, monkeypatch):
        provider = pd.LocalProcessesProvider()
        monkeypatch.setattr(pd.shutil, "which", lambda b: None)
        # Sem `ps` → fetcher raise; Cache devolve fallback (lista vazia).
        assert provider.get() == []
        assert "ps" in (provider.last_error or "")


class TestLocalLogsProvider:
    def test_returns_empty_when_file_missing(self, tmp_path):
        log = tmp_path / "missing.log"
        provider = pd.LocalLogsProvider(log)
        state = provider.get()
        assert state.events == []
        assert state.last_action_ts is None

    def test_tails_and_classifies(self, tmp_path):
        log = tmp_path / "deile.log"
        log.write_text(
            "2026-05-23 19:39:01,234 - deile.foo - INFO - hello\n"
            "2026-05-23 19:39:05,123 - deile.bar - INFO - "
            "deile.orchestration.pipeline.stages something happened\n"
            "2026-05-23 19:39:06,000 - deile.x - INFO - "
            "worker dispatch starting\n",
            encoding="utf-8",
        )
        provider = pd.LocalLogsProvider(log)
        state = provider.get()
        # Pelo menos 1 evento classificado (stages e dispatch reconhecidos).
        assert state.raw_lines >= 3
        assert state.events  # pelo menos 1 classificado
        actions = {ev.action for ev in state.events}
        assert {"stages", "dispatch"} & actions
        # Todos os eventos locais ganham actor='local'.
        assert all(ev.actor == "local" for ev in state.events)

    def test_tail_only_reads_last_64kb(self, tmp_path):
        log = tmp_path / "big.log"
        # Grava ~200KB de lixo + 1 linha boa no fim → garante que tail
        # leu só o final (não trava em arquivo grande).
        junk = "x" * 1000
        with log.open("w", encoding="utf-8") as fh:
            for _ in range(200):
                fh.write(junk + "\n")
            fh.write("2026-05-23 19:39:01,000 - x - INFO - "
                     "worker dispatch completed\n")
        provider = pd.LocalLogsProvider(log)
        state = provider.get()
        # File size em KB com divisão int — 200*1001 bytes ≈ 195KB.
        assert state.file_size_kb >= 100
        # A última linha (dispatch completed) DEVE ter sido capturada
        # apesar do arquivo ser muito maior que o tail de 64KB.
        assert any("dispatch" in ev.detail for ev in state.events)


class TestLocalAuditProvider:
    def test_parses_jsonl(self, tmp_path):
        audit = tmp_path / "security_audit.log"
        # 2 linhas puras de JSON + 1 inválida (skip) + 1 inline-puro
        # (`json.loads` aceita o JSON do brace ao fim porque o `}` é
        # final).
        audit.write_text(
            '{"event_type":"TOOL_EXECUTION","ts":"2026-05-23T19:39:01",'
            '"action":"k8s_status","result":"allowed"}\n'
            '{"event_type":"SECURITY_POLICY_CHANGED","ts":"2026-05-23T19:39:02",'
            '"action":"kubectl_set_env","result":"completed"}\n'
            'INVALID LINE\n'
            'prefix runtime - '
            '{"event_type":"TOOL_EXECUTION","result":"completed"}\n',
            encoding="utf-8",
        )
        provider = pd.LocalAuditProvider(audit)
        events = provider.get()
        assert len(events) == 3
        assert events[0]["event_type"] == "TOOL_EXECUTION"
        assert events[1]["event_type"] == "SECURITY_POLICY_CHANGED"
        # 3º veio do brace-extract — confirma o fallback funcional.
        assert events[2]["result"] == "completed"

    def test_handles_missing_file(self, tmp_path):
        provider = pd.LocalAuditProvider(tmp_path / "absent.log")
        assert provider.get() == []


class TestPanelDataFromContext:
    """`PanelData.from_context` wira providers com namespace override."""

    def test_k8s_namespace_propagates(self, tmp_path):
        # `k8s_force=True` força local_available=False sem depender da
        # ausência de logs/DB/processos no host (o teste pode rodar num
        # ambiente onde DEILE está rodando).
        ctx = pd.RuntimeContext(
            namespace="custom",
            pipeline_deploy="my-pipeline",
            worker_deploy="my-worker",
            bot_deploy="my-bot",
            usage_db=tmp_path / "u.db",
            logs_dir=tmp_path / "no-logs",
            k8s_force=True,
        )
        data = pd.PanelData.from_context(ctx)
        # local_available=False → providers locais não criados.
        assert data.local_processes is None
        assert data.local_logs is None
        assert data.local_audit is None
        # Namespace propagou.
        assert data.pods._namespace == "custom"
        assert data.pipeline._namespace == "custom"
        assert data.pipeline._deploy == "my-pipeline"
        assert data.workers._namespace == "custom"
        assert data.workers._worker_deploy == "my-worker"
        assert data.notifier._namespace == "custom"
        assert data.notifier._deploy == "my-bot"
        assert data.current_model.namespace == "custom"

    def test_local_providers_created_when_available(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(
            logs_dir=logs,
            usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        assert data.local_processes is not None
        assert data.local_logs is not None
        assert data.local_audit is not None

    def test_k8s_providers_disabled_in_local_only(self, monkeypatch, tmp_path):
        """`--local-only` (k8s_force=False + kubectl ausente OU local_force=True)
        deve fazer providers k8s retornarem fallback SEM chamar subprocess."""
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(
            local_force=True, logs_dir=logs, usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        # Intercepta `subprocess.run` para detectar QUALQUER chamada
        # (qualquer chamada significa que o `enabled=False` falhou).
        calls = []
        real_run = pd.subprocess.run
        def _trap(cmd, *a, **kw):
            calls.append(cmd)
            return real_run(cmd, *a, **kw)
        monkeypatch.setattr(pd.subprocess, "run", _trap)
        # Toca cada provider k8s — deve cair em fallback imediato.
        assert data.pods.get() == []
        assert data.pipeline.get().events == []
        assert data.workers.get() == {}
        assert data.notifier.get() == []
        # NENHUMA chamada kubectl deve ter saído.
        kubectl_calls = [c for c in calls if c and "kubectl" in str(c[0])]
        assert kubectl_calls == []
        # Errors filtrados: "k8s desabilitado" não vira alerta.
        assert data.errors() == []

    def test_set_preferred_model_uses_namespace(self, monkeypatch):
        """set_preferred_model com namespace custom deve passar pro kubectl."""
        monkeypatch.setattr(pd, "kubectl_bin", lambda: "/bin/kubectl")
        captured: List[List[str]] = []
        def _fake_run(cmd, **kw):
            captured.append(cmd)
            return MagicMock(returncode=0, stdout="ok\n", stderr="")
        with patch("subprocess.run", side_effect=_fake_run):
            ok, _ = pd.set_preferred_model(
                "deile-worker",
                "anthropic:claude-opus-4-7",
                namespace="my-namespace",
            )
        assert ok is True
        assert captured and captured[0][:3] == ["/bin/kubectl", "-n", "my-namespace"]


class TestDeployFlags:
    """Parser de flags do `deploy.py k8s panel` (universal mode)."""

    @pytest.fixture(scope="class", autouse=True)
    def _ensure_deploy_on_path(self):
        # `deploy.py` já está em `infra/k8s/` (mesmo dir do _panel_data) —
        # o sys.path setado no topo do arquivo cobre.
        yield

    def test_parses_value_flags(self):
        import deploy  # noqa: PLC0415
        ov, demo, standalone = deploy._parse_panel_flags(
            ["--namespace", "x", "--repo", "o/r", "--pipeline-deploy", "p"]
        )
        assert ov == {"namespace": "x", "repo": "o/r", "pipeline_deploy": "p"}
        assert demo is False
        assert standalone == {}

    def test_parses_bool_flags(self):
        import deploy  # noqa: PLC0415
        ov, demo, _ = deploy._parse_panel_flags(["--k8s-only"])
        assert ov == {"k8s_force": True}
        ov, _, _ = deploy._parse_panel_flags(["--local-only"])
        assert ov == {"local_force": True}
        ov, demo, _ = deploy._parse_panel_flags(["--demo"])
        assert ov == {}
        assert demo is True

    def test_paths_resolved(self):
        import deploy  # noqa: PLC0415
        ov, _, _ = deploy._parse_panel_flags(["--usage-db", "/tmp/u.db"])
        assert isinstance(ov["usage_db"], Path)
        assert str(ov["usage_db"]).endswith("u.db")

    def test_rejects_missing_value(self):
        import deploy  # noqa: PLC0415
        err, _, _ = deploy._parse_panel_flags(["--namespace"])
        assert "_error" in err
        assert "namespace" in err["_error"]

    def test_rejects_unknown_flag(self):
        import deploy  # noqa: PLC0415
        err, _, _ = deploy._parse_panel_flags(["--never-seen"])
        assert "_error" in err

    def test_parses_memdebug_standalone_flag(self):
        """`--memdebug` não é override de RuntimeContext; deve ir pro slot
        `standalone` (que `k8s_panel` passa pro `run_panel(memdebug=...)`)."""
        import deploy  # noqa: PLC0415
        ov, demo, standalone = deploy._parse_panel_flags(["--memdebug"])
        assert ov == {}
        assert demo is False
        assert standalone == {"memdebug": True}


# ===== Local instances (state files per PID — issue #303) ==================
#
# Testes do `LocalInstancesProvider`: lê arquivos JSON publicados por cada
# instância DEILE rodando no host. Cobre parsing, schema-version skip,
# GC de PID morto, override via env, e TTL.

def _write_state(dirpath: Path, instance_id: str, pid: int,
                 *, kind: str = "tool_execution",
                 detail: str = "execute_bash",
                 model: str = "deepseek:v4-pro",
                 heartbeat_age_s: float = 1.0,
                 schema_version: int = 1) -> Path:
    """Helper que escreve um state file no formato canônico do Agent A."""
    now = datetime.now(timezone.utc)
    hb = now - timedelta(seconds=heartbeat_age_s)
    started = now - timedelta(minutes=5)
    action_started = now - timedelta(seconds=2)
    if kind is None:
        action: Any = None
    else:
        action = {
            "kind": kind,
            "started_at": action_started.isoformat(),
            "detail": detail,
            "session_id": "sess-test",
            "model": model,
        }
    payload = {
        "schema_version": schema_version,
        "instance_id": instance_id,
        "pid": pid,
        "role": "cli",
        "started_at": started.isoformat(),
        "last_heartbeat_at": hb.isoformat(),
        "current_action": action,
        "stats": {
            "tokens_in": 100,
            "tokens_out": 50,
            "cost_usd": 0.01,
            "turns": 3,
            "tool_calls": 5,
            "errors": 0,
        },
    }
    path = dirpath / f"{instance_id}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class TestLocalInstancesProvider:
    """Lê `<runtime_dir>/*.json` (state files publicados por instância DEILE).

    Mocka `_pid_alive` com `monkeypatch` para os testes não dependerem
    de PIDs realmente existirem no host.
    """

    def test_returns_empty_when_dir_missing(self, tmp_path):
        missing = tmp_path / "no-such-dir"
        provider = pd.LocalInstancesProvider(runtime_dir=missing)
        assert provider.get() == {}
        assert provider.last_error is None  # dir ausente é caso normal

    def test_returns_empty_when_dir_exists_but_empty(self, tmp_path):
        rt = tmp_path / "run"
        rt.mkdir()
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        assert provider.get() == {}
        assert provider.last_error is None

    def test_parses_valid_state_file(self, tmp_path, monkeypatch):
        import json as _json  # ensure scoped
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-abc123", pid=12345,
                     kind="tool_execution", detail="execute_bash")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snaps = provider.get()
        assert set(snaps.keys()) == {12345}
        snap = snaps[12345]
        assert snap.instance_id == "cli-abc123"
        assert snap.pid == 12345
        assert snap.role == "cli"
        assert snap.current_action_kind == "tool_execution"
        assert snap.current_action_detail == "execute_bash"
        assert snap.current_action_model == "deepseek:v4-pro"
        assert snap.stats_tokens_in == 100
        assert snap.stats_cost_usd == pytest.approx(0.01)
        assert snap.stale is False
        # JSON ainda escrito (não foi GC'ed).
        assert (rt / "cli-abc123.json").is_file()
        _ = _json  # silenciar linter

    def test_skips_malformed_json(self, tmp_path, monkeypatch, caplog):
        rt = tmp_path / "run"
        rt.mkdir()
        (rt / "broken.json").write_text("{not valid json", encoding="utf-8")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        with caplog.at_level("WARNING"):
            snaps = provider.get()
        assert snaps == {}
        # Mensagem de warning emitida pelo provider — cobre `malformed`.
        assert any("malformed" in rec.message for rec in caplog.records)

    def test_skips_wrong_schema_version(self, tmp_path, monkeypatch, caplog):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-future", pid=999, schema_version=99)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        with caplog.at_level("WARNING"):
            snaps = provider.get()
        assert snaps == {}
        assert any("schema_version" in rec.message for rec in caplog.records)

    def test_garbage_collects_dead_pid(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        path = _write_state(rt, "cli-dead", pid=999999)
        # Simula PID morto.
        monkeypatch.setattr(pd, "_pid_alive", lambda p: False)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snaps = provider.get()
        assert snaps == {}
        # Arquivo deve ter sumido — GC silencioso.
        assert not path.exists()

    def test_marks_stale_when_old_heartbeat(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-slow", pid=12345, heartbeat_age_s=120.0)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(
            runtime_dir=rt, stale_after_s=30.0,
        )
        snaps = provider.get()
        assert 12345 in snaps
        assert snaps[12345].stale is True

    def test_idle_current_action_renders_idle(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        # `kind=None` → `_write_state` escreve `current_action: null`.
        _write_state(rt, "cli-idle", pid=12345, kind=None)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snap = provider.get()[12345]
        assert snap.current_action_kind == "idle"
        assert snap.current_action_detail == ""
        assert snap.doing_now_label == "idle"

    def test_tool_execution_renders_with_detail(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-toolex", pid=12345,
                     kind="tool_execution", detail="read_file")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snap = provider.get()[12345]
        # Texto puro (sem emoji): "tool: <detail>"
        assert snap.doing_now_label == "tool: read_file"

    def test_llm_call_renders_with_model(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-llm", pid=12345,
                     kind="llm_call", detail="completion",
                     model="anthropic:claude-opus-4-7")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snap = provider.get()[12345]
        assert snap.doing_now_label == "llm: anthropic:claude-opus-4-7"

    def test_runtime_dir_env_override(self, tmp_path, monkeypatch):
        rt = tmp_path / "custom-run"
        rt.mkdir()
        _write_state(rt, "cli-env", pid=12345)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        monkeypatch.setenv("DEILE_RUNTIME_DIR", str(rt))
        provider = pd.LocalInstancesProvider()  # sem runtime_dir explícito
        assert provider.runtime_dir == rt
        assert 12345 in provider.get()

    def test_cache_ttl_respected(self, tmp_path, monkeypatch):
        """Após o 1º fetch, get() não re-lê o filesystem dentro do TTL."""
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-cache", pid=12345)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt, ttl_s=60.0)
        # 1º fetch popula o cache.
        snaps1 = provider.get()
        assert 12345 in snaps1
        # Spy: substitui o _fetch para detectar nova chamada.
        calls: List[int] = []
        real_fetch = provider._fetch
        def _spy():
            calls.append(1)
            return real_fetch()
        provider._cache.fetcher = _spy
        # 2º get dentro do TTL não chama _fetch (cache válido).
        snaps2 = provider.get()
        assert calls == []
        assert snaps2 == snaps1

    def test_starting_kind_label(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-start", pid=12345,
                     kind="starting", detail="")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        assert provider.get()[12345].doing_now_label == "starting…"

    def test_shutting_down_kind_label(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-shut", pid=12345,
                     kind="shutting_down", detail="")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        assert provider.get()[12345].doing_now_label == "shutting down"

    def test_unknown_kind_falls_back_to_generic_label(self, tmp_path,
                                                     monkeypatch):
        """Forward-compat: kind futuro não conhecido vira `<kind>: <detail>`."""
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-future-kind", pid=12345,
                     kind="quantum_compute", detail="qubit-42")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snap = provider.get()[12345]
        assert "quantum_compute" in snap.doing_now_label
        assert "qubit-42" in snap.doing_now_label

    def test_multiple_pids_all_indexed(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        _write_state(rt, "cli-a", pid=1001,
                     kind="tool_execution", detail="execute_bash")
        _write_state(rt, "cli-b", pid=2002,
                     kind="llm_call", model="openai:gpt-5")
        _write_state(rt, "cli-c", pid=3003, kind=None)
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        snaps = provider.get()
        assert set(snaps.keys()) == {1001, 2002, 3003}
        # Cada PID com seu próprio doing_now_label — assinatura do fix.
        labels = {pid: snap.doing_now_label for pid, snap in snaps.items()}
        assert labels[1001] == "tool: execute_bash"
        assert labels[2002] == "llm: openai:gpt-5"
        assert labels[3003] == "idle"
        # Os 3 labels DEVEM ser diferentes — o bug que esta feature resolve.
        assert len(set(labels.values())) == 3

    def test_listdir_failure_propagates_as_last_error(self, tmp_path,
                                                     monkeypatch):
        """Diretório com permissão negada → last_error preenchido."""
        rt = tmp_path / "run"
        rt.mkdir()
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        def _explode(self):
            raise OSError("permission denied")
        monkeypatch.setattr(pd.Path, "glob", _explode)
        snaps = provider.get(force=True)
        assert snaps == {}
        assert provider.last_error is not None
        assert "listdir" in provider.last_error

    def test_missing_pid_in_payload_skipped(self, tmp_path, monkeypatch):
        rt = tmp_path / "run"
        rt.mkdir()
        # JSON válido mas sem `pid` — provider pula silenciosamente
        # (log WARNING).
        payload = {"schema_version": 1, "instance_id": "no-pid"}
        (rt / "no-pid.json").write_text(json.dumps(payload),
                                        encoding="utf-8")
        monkeypatch.setattr(pd, "_pid_alive", lambda p: True)
        provider = pd.LocalInstancesProvider(runtime_dir=rt)
        assert provider.get() == {}


class TestLocalInstancesInPanelData:
    """`local_instances` é wired-up corretamente no `PanelData.from_context`."""

    def test_from_context_creates_local_instances_when_available(self,
                                                                 tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(
            logs_dir=logs, usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        assert data.local_instances is not None
        assert isinstance(data.local_instances, pd.LocalInstancesProvider)

    def test_local_instances_none_when_not_available(self, tmp_path):
        ctx = pd.RuntimeContext(
            k8s_force=True,
            logs_dir=tmp_path / "no-logs",
            usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        assert data.local_instances is None

    def test_local_instances_in_all_providers(self, tmp_path):
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(
            logs_dir=logs, usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        providers = data._all_providers()
        assert data.local_instances in providers

    def test_local_instances_in_errors_list(self, tmp_path, monkeypatch):
        logs = tmp_path / "logs"
        logs.mkdir()
        ctx = pd.RuntimeContext(
            logs_dir=logs, usage_db=tmp_path / "u.db",
        )
        data = pd.PanelData.from_context(ctx)
        # Força um erro no provider via Path.glob monkeypatch.
        def _explode(self):
            raise OSError("forced for test")
        monkeypatch.setattr(pd.Path, "glob", _explode)
        # Cache cold → get() chama _fetch → set last_error.
        data.local_instances.get(force=True)
        names = [n for n, _ in data.errors()]
        assert "local_instances" in names


class TestPanelShowsPerPidAction:
    """Render integration: linhas da tabela LOCAL PROCESSES têm `doing now`
    DIFERENTE por PID quando há snapshots — oposto do bug original
    (todas mostravam o mesmo texto vindo do log global).
    """

    def test_dashboard_shows_per_pid_action_when_instance_snapshot_present(
        self, monkeypatch,
    ):
        # Mocka PanelData com 2 PIDs locais, cada um com seu snapshot.
        data = MagicMock()
        data.context = MagicMock()
        # 2 processos locais.
        p1 = pd.LocalProcessInfo(
            pid=28117, role="local-deile",
            cmd="python3 deile.py", cpu_pct=0.0, rss_kb=25000, etime_s=1800,
        )
        p2 = pd.LocalProcessInfo(
            pid=16694, role="local-deile",
            cmd="python3 deile.py", cpu_pct=0.0, rss_kb=14000, etime_s=6700,
        )
        data.local_processes.get.return_value = [p1, p2]
        # 2 snapshots com `current_action` diferentes.
        now = datetime.now(timezone.utc)
        snap1 = pd.InstanceSnapshot(
            instance_id="cli-a", pid=28117, role="cli",
            started_at=now, last_heartbeat_at=now,
            current_action_kind="tool_execution",
            current_action_detail="execute_bash",
            current_action_started_at=now,
            current_action_model="",
            stats_tokens_in=0, stats_tokens_out=0, stats_cost_usd=0.0,
            stats_turns=0, stats_tool_calls=0, stats_errors=0,
            stale=False,
        )
        snap2 = pd.InstanceSnapshot(
            instance_id="cli-b", pid=16694, role="cli",
            started_at=now, last_heartbeat_at=now,
            current_action_kind="llm_call",
            current_action_detail="completion",
            current_action_started_at=now,
            current_action_model="anthropic:claude-opus-4-7",
            stats_tokens_in=0, stats_tokens_out=0, stats_cost_usd=0.0,
            stats_turns=0, stats_tool_calls=0, stats_errors=0,
            stale=False,
        )
        data.local_instances.get.return_value = {28117: snap1, 16694: snap2}
        # `local_logs` ainda retorna o estado global (que era usado como
        # fallback antes do fix) — propositalmente o mesmo texto pra
        # provar que o per-PID snapshot vence.
        log_state = MagicMock()
        log_state.last_action_age_s = 10
        log_state.last_action_summary = "worker dispatch completed"
        data.local_logs.get.return_value = log_state
        rows = panel._local_process_rows(data)
        assert len(rows) == 2
        # ASSINATURA DO FIX: doing now de cada linha é diferente.
        doings = [r.doing_now for r in rows]
        assert len(set(doings)) == 2
        # Nenhum dos dois deve ser o texto do log global (fallback antigo).
        assert "worker dispatch completed" not in doings
        # Por PID: 28117 → tool, 16694 → llm.
        by_pid = {r.name: r.doing_now for r in rows}
        assert "tool: execute_bash" in by_pid["local-deile#28117"]
        assert "llm: anthropic:claude-opus-4-7" in by_pid["local-deile#16694"]

    def test_falls_back_to_log_state_when_no_snapshot(self, monkeypatch):
        """PID sem state file (compat com processos legacy) → fallback log."""
        data = MagicMock()
        p = pd.LocalProcessInfo(
            pid=99999, role="local-other",
            cmd="python3 deile.py", cpu_pct=0.0, rss_kb=1000, etime_s=60,
        )
        data.local_processes.get.return_value = [p]
        # local_instances vazio — PID 99999 não tem snapshot.
        data.local_instances.get.return_value = {}
        log_state = MagicMock()
        log_state.last_action_age_s = 5
        log_state.last_action_summary = "legacy log message"
        data.local_logs.get.return_value = log_state
        rows = panel._local_process_rows(data)
        assert len(rows) == 1
        assert "legacy log message" in rows[0].doing_now

    def test_falls_back_to_cmd_when_no_instances_and_no_log(self):
        """Pior caso: sem snapshot e sem log → cmdline + busy via CPU."""
        data = MagicMock()
        p = pd.LocalProcessInfo(
            pid=88888, role="local-other",
            cmd="python3 deile.py --special", cpu_pct=2.0, rss_kb=1000,
            etime_s=60,
        )
        data.local_processes.get.return_value = [p]
        data.local_instances.get.return_value = {}
        # local_logs presente mas state sem last_action_age_s.
        log_state = MagicMock()
        log_state.last_action_age_s = None
        log_state.last_action_summary = ""
        data.local_logs.get.return_value = log_state
        rows = panel._local_process_rows(data)
        assert "deile.py" in rows[0].doing_now
        # CPU >= 1% → busy=True.
        assert rows[0].busy is True

    def test_no_local_instances_attribute_does_not_crash(self):
        """Smoke: PanelData sem local_instances (modo k8s-only) não crasha."""
        data = MagicMock(spec=["local_processes", "local_logs"])
        p = pd.LocalProcessInfo(
            pid=12345, role="local-deile",
            cmd="python3 deile.py", cpu_pct=0.0, rss_kb=1000, etime_s=60,
        )
        data.local_processes.get.return_value = [p]
        log_state = MagicMock()
        log_state.last_action_age_s = None
        log_state.last_action_summary = ""
        data.local_logs.get.return_value = log_state
        # Sem AttributeError — getattr default {} é usado.
        rows = panel._local_process_rows(data)
        assert len(rows) == 1


# ===== StageDispatchProvider (issue #309 fase 2 — Task 17) ==================

def _deployment_with_env(envs: Dict[str, str]) -> Dict[str, Any]:
    """Builda um kubectl-get-deployment JSON minimal com as env vars dadas."""
    env_list = [{"name": k, "value": v} for k, v in envs.items()]
    return {"spec": {"template": {"spec": {"containers": [{"env": env_list}]}}}}


class TestStageDispatchProvider:
    """Consolidador per-stage de worker + model + status do claude-worker.

    Lê de DOIS Deployments (``deile-pipeline`` para worker dispatch,
    ``deile-worker`` para models) + UM Secret (``claude-credentials`` para
    email). Mock ``_capture_json`` para evitar cluster real.
    """

    @staticmethod
    def _route_capture(payloads: Dict[str, Any]):
        """Side-effect factory: roteia subprocess args para o payload certo.

        ``payloads`` keys são os argv tokens distintivos (ex.:
        ``"deployment/deile-pipeline"``); valor é o dict JSON a devolver
        (ou ``None`` para simular fetch falho).
        """
        def fake(cmd, timeout=None):
            for token, payload in payloads.items():
                if token in cmd:
                    return payload
            return None
        return fake

    def test_returns_five_entries_one_per_stage(self):
        from _panel_data import StageDispatchProvider

        from deile.orchestration.pipeline.dispatch_resolver import \
            PIPELINE_STAGES
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({}),
            "deployment/deile-worker": _deployment_with_env({}),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageDispatchProvider().get_all_stages(force=True)
        assert len(entries) == 5
        assert [e.stage for e in entries] == list(PIPELINE_STAGES)
        # Sem nenhum env, todos caem no default.
        for e in entries:
            assert e.worker == "deile-worker"
            assert e.source == "default"
            assert e.model is None

    def test_per_stage_worker_env_takes_precedence(self):
        """``DEILE_PIPELINE_DISPATCH_<STAGE>`` vence o global ``DISPATCH_MODE``."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({
                "DEILE_PIPELINE_DISPATCH_IMPLEMENT": "claude-worker",
                "DEILE_PIPELINE_DISPATCH_MODE": "deile-worker",
            }),
            "deployment/deile-worker": _deployment_with_env({}),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            by_stage = {
                e.stage: e
                for e in StageDispatchProvider().get_all_stages(force=True)
            }
        # implement: per-stage env wins.
        assert by_stage["implement"].worker == "claude-worker"
        assert by_stage["implement"].source == "env"
        # classify: cai no global DISPATCH_MODE.
        assert by_stage["classify"].worker == "deile-worker"
        assert by_stage["classify"].source == "global"

    def test_per_stage_model_env_takes_precedence(self):
        """``DEILE_PIPELINE_MODEL_<STAGE>`` vence o global ``DEILE_PIPELINE_MODEL``."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({}),
            "deployment/deile-worker": _deployment_with_env({
                "DEILE_PIPELINE_MODEL_IMPLEMENT": "anthropic:claude-opus-4-7",
                "DEILE_PIPELINE_MODEL": "anthropic:claude-sonnet-4-6",
            }),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            by_stage = {
                e.stage: e
                for e in StageDispatchProvider().get_all_stages(force=True)
            }
        # implement: per-stage env wins.
        assert by_stage["implement"].model == "anthropic:claude-opus-4-7"
        # refine: cai no global DEILE_PIPELINE_MODEL.
        assert by_stage["refine"].model == "anthropic:claude-sonnet-4-6"

    def test_preferred_model_aliases_pipeline_model(self):
        """``DEILE_PREFERRED_MODEL`` é aceito como alias do model global."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({}),
            "deployment/deile-worker": _deployment_with_env({
                "DEILE_PREFERRED_MODEL": "deepseek:deepseek-v4-pro",
            }),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageDispatchProvider().get_all_stages(force=True)
        for e in entries:
            assert e.model == "deepseek:deepseek-v4-pro"

    def test_combined_per_stage_worker_and_model(self):
        """Per-stage env de worker E model setados, source='env' no worker."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({
                "DEILE_PIPELINE_DISPATCH_IMPLEMENT": "claude-worker",
            }),
            "deployment/deile-worker": _deployment_with_env({
                "DEILE_PIPELINE_MODEL_IMPLEMENT": "anthropic:claude-opus-4-7",
            }),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            by_stage = {
                e.stage: e
                for e in StageDispatchProvider().get_all_stages(force=True)
            }
        assert by_stage["implement"].worker == "claude-worker"
        assert by_stage["implement"].model == "anthropic:claude-opus-4-7"
        assert by_stage["implement"].source == "env"

    def test_canonicalizes_legacy_worker_aliases(self):
        """``deile_worker`` (underscore) vira ``deile-worker`` na view."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({
                "DEILE_PIPELINE_DISPATCH_MODE": "deile_worker",
            }),
            "deployment/deile-worker": _deployment_with_env({}),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageDispatchProvider().get_all_stages(force=True)
        for e in entries:
            assert e.worker == "deile-worker"
            assert e.source == "global"

    def test_blank_per_stage_env_falls_back_to_global(self):
        """Env presente com value vazio é tratado como ausente."""
        from _panel_data import StageDispatchProvider
        payloads = {
            "deployment/deile-pipeline": _deployment_with_env({
                "DEILE_PIPELINE_DISPATCH_IMPLEMENT": "   ",
                "DEILE_PIPELINE_DISPATCH_MODE": "claude-worker",
            }),
            "deployment/deile-worker": _deployment_with_env({}),
        }
        with patch("_panel_data._capture_json",
                   side_effect=self._route_capture(payloads)), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            by_stage = {
                e.stage: e
                for e in StageDispatchProvider().get_all_stages(force=True)
            }
        assert by_stage["implement"].worker == "claude-worker"
        assert by_stage["implement"].source == "global"

    def test_disabled_returns_five_default_entries(self):
        """``enabled=False`` (modo --local-only) → 5 stages default sem kubectl."""
        from _panel_data import StageDispatchProvider
        with patch("_panel_data._capture_json") as fake_capture:
            entries = StageDispatchProvider(enabled=False).get_all_stages(
                force=True,
            )
            fake_capture.assert_not_called()
        assert len(entries) == 5
        for e in entries:
            assert e.worker == "deile-worker"
            assert e.source == "default"
            assert e.model is None

    def test_pipeline_deployment_absent_falls_back_to_default(self):
        """Pipeline Deployment ausente → 5 stages default (não levanta)."""
        from _panel_data import StageDispatchProvider
        with patch("_panel_data._capture_json", return_value=None), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            entries = StageDispatchProvider().get_all_stages(force=True)
        assert len(entries) == 5
        for e in entries:
            assert e.worker == "deile-worker"
            assert e.source == "default"


class TestClaudeWorkerStatus:
    """``get_claude_worker_status`` consolida Deployment + Secret."""

    def test_deployment_absent_returns_not_applied(self):
        from _panel_data import StageDispatchProvider
        with patch("_panel_data._capture_json", return_value=None), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            status = StageDispatchProvider().get_claude_worker_status(
                force=True,
            )
        assert status.deployment_applied is False
        assert status.pod_ready is False
        assert status.logged_in_email is None

    def test_deployment_ready_with_email(self):
        """Deployment com readyReplicas == replicas + Secret válido."""
        import base64
        import json as _json

        from _panel_data import StageDispatchProvider
        fake_deployment = {"status": {"readyReplicas": 1, "replicas": 1}}
        creds_b64 = base64.b64encode(
            _json.dumps({"email": "user@example.com"}).encode()
        ).decode()
        fake_secret = {"data": {"credentials.json": creds_b64}}

        def route(cmd, timeout=None):
            if "deployment/claude-worker" in cmd:
                return fake_deployment
            if "secret/claude-credentials" in cmd:
                return fake_secret
            return None

        with patch("_panel_data._capture_json", side_effect=route), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            status = StageDispatchProvider().get_claude_worker_status(
                force=True,
            )
        assert status.deployment_applied is True
        assert status.pod_ready is True
        assert status.logged_in_email == "user@example.com"

    def test_deployment_applied_but_pod_not_ready(self):
        """Deployment aplicado mas pod ainda subindo (readyReplicas < replicas)."""
        from _panel_data import StageDispatchProvider
        fake_deployment = {"status": {"readyReplicas": 0, "replicas": 1}}

        def route(cmd, timeout=None):
            if "deployment/claude-worker" in cmd:
                return fake_deployment
            return None

        with patch("_panel_data._capture_json", side_effect=route), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            status = StageDispatchProvider().get_claude_worker_status(
                force=True,
            )
        assert status.deployment_applied is True
        assert status.pod_ready is False
        assert status.logged_in_email is None

    def test_secret_malformed_email_returns_none(self):
        """Secret presente mas base64/JSON malformado → email None silencioso."""
        from _panel_data import StageDispatchProvider
        fake_deployment = {"status": {"readyReplicas": 1, "replicas": 1}}
        # base64 inválido → ValueError dentro do helper.
        fake_secret = {"data": {"credentials.json": "not-base64-!!"}}

        def route(cmd, timeout=None):
            if "deployment/claude-worker" in cmd:
                return fake_deployment
            if "secret/claude-credentials" in cmd:
                return fake_secret
            return None

        with patch("_panel_data._capture_json", side_effect=route), \
             patch("_panel_data.kubectl_bin", return_value="/fake/kubectl"):
            status = StageDispatchProvider().get_claude_worker_status(
                force=True,
            )
        # Deployment ainda ready; email cai pra None.
        assert status.deployment_applied is True
        assert status.pod_ready is True
        assert status.logged_in_email is None

    def test_disabled_returns_neutral_status(self):
        """``enabled=False`` → status neutro sem chamar kubectl."""
        from _panel_data import StageDispatchProvider
        with patch("_panel_data._capture_json") as fake_capture:
            status = StageDispatchProvider(
                enabled=False,
            ).get_claude_worker_status(force=True)
            fake_capture.assert_not_called()
        assert status.deployment_applied is False
        assert status.pod_ready is False
        assert status.logged_in_email is None
