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

import _panel_data as pd  # noqa: E402
import _panel as panel  # noqa: E402


# ===== Cache TTL ============================================================

class TestCache:
    def test_first_call_invokes_fetcher(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        assert cache.get() == "ok"
        assert calls == [1]

    def test_second_call_within_ttl_uses_cache(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()
        cache.get()
        cache.get()
        assert calls == [1]

    def test_force_bypasses_ttl(self):
        calls: List[int] = []
        cache = pd.Cache(ttl_s=10.0, fetcher=lambda: calls.append(1) or "ok",
                         fallback="-")
        cache.get()
        cache.get(force=True)
        assert calls == [1, 1]

    def test_error_keeps_last_value_and_records_error(self):
        attempts = {"n": 0}

        def fetcher() -> str:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return "good"
            raise RuntimeError("boom")

        cache = pd.Cache(ttl_s=0.0, fetcher=fetcher, fallback="fallback")
        assert cache.get() == "good"
        # Segunda chamada (TTL=0) refaz e falha — mantém o último valor bom.
        assert cache.get() == "good"
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

    def test_no_kubectl_returns_fallback(self):
        prov = pd.PodsProvider(ttl_s=10.0)
        prov._kubectl = None
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
