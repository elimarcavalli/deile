"""Tests for _classify_pipeline_line canonical families (issue #448).

Covers AC1–AC8 from the issue spec:
  AC1  — all 15 subtypes recognized, actions match spec table
  AC2  — unknown family falls through to legacy (returns None for unmatched)
  AC3  — additive-only extra k=v keys appear in detail
  AC4  — lines >500 chars dropped (line_too_long counter)
  AC5  — \t / \r control chars dropped (line_has_forbidden_char counter)
  AC6  — secrets redacted from detail
  AC7  — latency: _parse(<200 lines>) < 50ms
  AC8  — target derivation per family
"""
from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))

from _panel_data import (  # noqa: E402
    ActivityEvent,
    LogLine,
    PipelineProvider,
    PipelineState,
    _classify_pipeline_line,
    _parse_canonical_kv,
    _redact_canonical_detail,
)

_UTC = timezone.utc
_TS = datetime(2026, 1, 1, 12, 0, 0, tzinfo=_UTC)


def _ll(body: str) -> LogLine:
    return LogLine(ts=_TS, body=body)


def _cls(body: str):
    return _classify_pipeline_line(_ll(body))


# ---------------------------------------------------------------------------
# AC1 — 15 subtypes recognised, actions are exact strings from spec table
# ---------------------------------------------------------------------------

_CANONICAL_FIXTURES = [
    ("refinement.critique issue=42 persona=architect verdict=VAGO gaps='scope unclear'",
     "refinement.critique", "#42"),
    ("refinement.refine issue=42 round=1 persona=analyst body_chars=300 verdict=CLARO",
     "refinement.refine", "#42"),
    ("decomposition.fanout intent=10 derivadas=[11,12] complexity=[M,S]",
     "decomposition.fanout", "#10"),
    ("batch.claim sha=abc1234ef0 issues=[1,2] reason=shard",
     "batch.claim", "~batch:abc1234e"),
    ("batch.release sha=abc1234ef0 reason=done",
     "batch.release", "~batch:abc1234e"),
    ("label.change target_kind=issue target=42 removed=[] added=[~workflow:revisada]",
     "label.change", "#42"),
    ("label.change target_kind=pr target=99 removed=[] added=[~review:concluida]",
     "label.change", "PR#99"),
    ("reaper.unblock target_kind=issue target=7 attempts=1 reason=fresh",
     "reaper.unblock", "#7"),
    ("reaper.block target_kind=pr target=7 attempts=3 cap=5 reason=stale",
     "reaper.block", "PR#7"),
    ("auth.fail target=worker-abc attempts=3 thr=3 reason=expired",
     "auth.fail", "worker-abc"),
    ("auth.backoff target=worker-abc attempts=4 backoff_s=120 until=2026-01-01T12:02:00Z",
     "auth.backoff", "worker-abc"),
    ("auth.skip target=worker-abc remaining_s=118",
     "auth.skip", "worker-abc"),
    ("auth.recover target=worker-abc reason=renewed",
     "auth.recover", "worker-abc"),
    ("routing.mention target_kind=issue target=42 action=injected_nova",
     "routing.mention", "#42"),
    ("routing.pr_unified target=99 role=assignee",
     "routing.pr_unified", "PR#99"),
    ("routing.dropped target_kind=issue target=42 reason=eco",
     "routing.dropped", "#42"),
]


class TestAC1AllActionsRecognised:
    def test_all_15_actions_present(self):
        events = [_cls(body) for body, _, _ in _CANONICAL_FIXTURES]
        assert all(ev is not None for ev in events), "Some fixtures returned None"
        actions = {ev.action for ev in events if ev is not None}
        expected = {
            "refinement.critique", "refinement.refine",
            "decomposition.fanout",
            "batch.claim", "batch.release",
            "label.change",
            "reaper.unblock", "reaper.block",
            "auth.fail", "auth.backoff", "auth.skip", "auth.recover",
            "routing.mention", "routing.pr_unified", "routing.dropped",
        }
        assert actions == expected

    @pytest.mark.parametrize("body,expected_action,_target", _CANONICAL_FIXTURES)
    def test_action_exact(self, body, expected_action, _target):
        ev = _cls(body)
        assert ev is not None
        assert ev.action == expected_action


# ---------------------------------------------------------------------------
# AC2 — unknown family falls through to legacy (no canonical_dropped increment)
# ---------------------------------------------------------------------------

class TestAC2UnknownFamilyFallthrough:
    def test_unknown_family_returns_none(self):
        ev = _cls("unknown.thing key=val")
        assert ev is None

    def test_unknown_family_not_in_canonical_families(self):
        result = _parse_canonical_kv("unknown.thing key=val")
        assert result is None

    def test_partial_prefix_does_not_match(self):
        # "refinementx.critique" is not in the frozenset
        result = _parse_canonical_kv("refinementx.critique issue=1 persona=a verdict=b")
        assert result is None


# ---------------------------------------------------------------------------
# AC3 — extra k=v keys preserved in detail
# ---------------------------------------------------------------------------

class TestAC3AdditivekV:
    def test_extra_key_in_detail(self):
        body = "refinement.critique issue=42 persona=architect verdict=VAGO gaps='foo bar' NOVO_KEY=xyz"
        ev = _cls(body)
        assert ev is not None
        assert "NOVO_KEY=xyz" in ev.detail

    def test_extra_key_in_decomposition(self):
        body = "decomposition.fanout intent=5 derivadas=[6,7] complexity=[M,S] new_field=hello"
        ev = _cls(body)
        assert ev is not None
        assert "new_field=hello" in ev.detail


# ---------------------------------------------------------------------------
# AC4 — lines >500 chars rejected (line_too_long)
# ---------------------------------------------------------------------------

class TestAC4LineTooLong:
    def _make_provider_parse(self, lines):
        """Build minimal kubectl log text and run _parse."""
        ts_prefix = "2026-01-01T12:00:00.000000000Z "
        text = "\n".join(ts_prefix + ln for ln in lines)
        provider = PipelineProvider.__new__(PipelineProvider)
        return provider._parse(text)

    def test_line_over_500_dropped(self):
        long_body = "refinement.critique " + "x" * 482  # total > 500
        assert len(long_body) > 500
        state = self._make_provider_parse([long_body])
        assert state.canonical_dropped["line_too_long"] == 1
        assert len(state.events) == 0

    def test_line_exactly_500_not_dropped(self):
        # body of exactly 500 chars — NOT dropped (condition is >500)
        body = "refinement.critique issue=1 persona=a verdict=b " + "x" * (500 - 48)
        assert len(body) == 500
        state = self._make_provider_parse([body])
        assert state.canonical_dropped["line_too_long"] == 0


# ---------------------------------------------------------------------------
# AC5 — \t / \r control chars rejected (line_has_forbidden_char)
# ---------------------------------------------------------------------------

class TestAC5ForbiddenChars:
    def _make_provider_parse(self, lines):
        ts_prefix = "2026-01-01T12:00:00.000000000Z "
        text = "\n".join(ts_prefix + ln for ln in lines)
        provider = PipelineProvider.__new__(PipelineProvider)
        return provider._parse(text)

    def _parse_with_fake_lls(self, bodies):
        """Inject LogLine objects directly into _parse, bypassing splitlines().
        Needed for \\r tests: splitlines() consumes \\r as a line separator so
        \\r can never reach ll.body via the normal text path.
        """
        from unittest.mock import patch
        fakes = [LogLine(ts=_TS, body=b) for b in bodies]
        idx = [-1]

        def _fake_parse_log_line(raw):
            idx[0] += 1
            return fakes[idx[0]] if idx[0] < len(fakes) else None

        provider = PipelineProvider.__new__(PipelineProvider)
        dummy_text = "\n".join("dummy" for _ in bodies)
        with patch("_panel_data._parse_log_line", side_effect=_fake_parse_log_line):
            return provider._parse(dummy_text)

    def test_tab_rejected(self):
        body = "refinement.critique\tissue=1 persona=a verdict=b"
        state = self._make_provider_parse([body])
        assert state.canonical_dropped["line_has_forbidden_char"] >= 1

    def test_cr_rejected(self):
        # \r is consumed by splitlines() — inject LogLine with \r in body directly.
        state = self._parse_with_fake_lls(
            ["auth.fail\rtarget=w attempts=1 thr=3 reason=x"]
        )
        assert state.canonical_dropped["line_has_forbidden_char"] >= 1

    def test_both_rejected_increments_twice(self):
        # One tab line (via normal path) + one CR line (via injected path).
        tab_body = "auth.fail\ttarget=w1 attempts=1 thr=3 reason=x"
        cr_body = "auth.fail\rtarget=w2 attempts=1 thr=3 reason=x"
        # Tab via normal parse
        tab_state = self._make_provider_parse([tab_body])
        # CR via injected LogLine
        cr_state = self._parse_with_fake_lls([cr_body])
        total = (tab_state.canonical_dropped["line_has_forbidden_char"]
                 + cr_state.canonical_dropped["line_has_forbidden_char"])
        assert total == 2


# ---------------------------------------------------------------------------
# AC6 — secrets redacted from detail
# ---------------------------------------------------------------------------

class TestAC6SecretsRedacted:
    def test_token_redacted(self):
        ev = _cls("auth.fail target=worker-1 token=ghp_AAAAAAAAAA reason=expired")
        assert ev is not None
        assert "ghp_AAAAAAAAAA" not in ev.detail
        assert "<redacted>" in ev.detail

    def test_bearer_redacted(self):
        s = _redact_canonical_detail("bearer=abc123xyz")
        assert "abc123xyz" not in s
        assert "<redacted>" in s

    def test_api_key_redacted(self):
        s = _redact_canonical_detail("api_key=sk-secret123")
        assert "sk-secret123" not in s
        assert "<redacted>" in s

    def test_secret_redacted(self):
        s = _redact_canonical_detail("secret=topsecret")
        assert "topsecret" not in s
        assert "<redacted>" in s

    def test_password_redacted(self):
        s = _redact_canonical_detail("password=hunter2")
        assert "hunter2" not in s
        assert "<redacted>" in s

    def test_authorization_redacted(self):
        s = _redact_canonical_detail("authorization=Bearer_some_token")
        assert "Bearer_some_token" not in s
        assert "<redacted>" in s

    def test_normal_reason_not_redacted(self):
        ev = _cls("auth.recover target=worker-abc reason=renewed")
        assert ev is not None
        assert "renewed" in ev.detail


# ---------------------------------------------------------------------------
# AC7 — latency: _parse(200 lines) < 50ms
# ---------------------------------------------------------------------------

@pytest.mark.skipif(
    os.getenv("DEILE_PERF_TEST") == "0",
    reason="Performance tests disabled via DEILE_PERF_TEST=0",
)
class TestAC7Latency:
    def test_parse_200_lines_under_50ms(self):
        ts_prefix = "2026-01-01T12:00:00.000000000Z "
        canonical = [
            "refinement.critique issue=42 persona=architect verdict=VAGO",
            "refinement.refine issue=42 round=1 persona=analyst body_chars=100 verdict=CLARO",
            "batch.claim sha=abc1234ef issues=[1,2] reason=shard",
            "auth.fail target=worker-1 attempts=3 thr=3 reason=expired",
            "routing.mention target_kind=issue target=10 action=injected_nova",
        ]
        legacy = [
            "mention group issue:5: triggers=['nova']",
            "worker dispatch starting",
            "deile.orchestration.pipeline.stages claim done",
            "starting pipeline monitor",
            "unrecognised line of noise xyz 123",
        ]
        noise = ["completely unrelated log line number " + str(i) for i in range(80)]
        # 60 canonical (12 per pattern × 5) + 60 legacy (12 per × 5) + 80 noise = 200
        lines = (canonical * 12 + legacy * 12 + noise)[:200]
        text = "\n".join(ts_prefix + ln for ln in lines)
        provider = PipelineProvider.__new__(PipelineProvider)
        t0 = time.perf_counter()
        provider._parse(text)
        elapsed_ms = (time.perf_counter() - t0) * 1000
        assert elapsed_ms < 50, f"_parse took {elapsed_ms:.1f}ms — exceeds 50ms limit"


# ---------------------------------------------------------------------------
# AC8 — target derivation per family
# ---------------------------------------------------------------------------

class TestAC8TargetDerivation:
    @pytest.mark.parametrize("body,_action,expected_target", _CANONICAL_FIXTURES)
    def test_target_matches_spec(self, body, _action, expected_target):
        ev = _cls(body)
        assert ev is not None
        assert ev.target == expected_target, (
            f"family={ev.action}: expected target={expected_target!r}, got {ev.target!r}"
        )

    def test_routing_pr_unified_target(self):
        ev = _cls("routing.pr_unified target=91 role=reviewer")
        assert ev is not None
        assert ev.target == "PR#91"

    def test_batch_sha_truncated_to_8(self):
        ev = _cls("batch.claim sha=abcdef1234567890 issues=[1] reason=x")
        assert ev is not None
        assert ev.target == "~batch:abcdef12"

    def test_missing_required_key_parse_partial(self):
        # refinement.critique without issue= → target="" and [parse_partial] in detail
        ev = _cls("refinement.critique persona=architect verdict=VAGO")
        assert ev is not None
        assert ev.target == ""
        assert "[parse_partial]" in ev.detail
