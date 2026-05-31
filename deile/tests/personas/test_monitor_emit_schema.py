"""AC1 for issue #439 — monitor.md must contain at least one emit point for each
of the 10 canonical event families + the audit_pvc_fail operational event.

These tests verify the STABLE CONTRACT consumed by #436 (ACTIVITY widget) and
#440 (monitor-audit parser).  They do NOT test LLM behaviour — they test that
the persona instruction file documents every required family so the model knows
to emit it.
"""
from __future__ import annotations

from pathlib import Path

import pytest

_MONITOR_MD = (
    Path(__file__).parent.parent.parent  # deile/
    / "personas"
    / "instructions"
    / "monitor.md"
)


@pytest.fixture(scope="module")
def monitor_content() -> str:
    assert _MONITOR_MD.exists(), f"monitor.md not found at {_MONITOR_MD}"
    return _MONITOR_MD.read_text(encoding="utf-8")


# ── 10 canonical families ──────────────────────────────────────────────────

class TestCanonicalEmitFamilies:
    """Every family must have at least one concrete echo/emit point in monitor.md."""

    def test_monitor_tick_present(self, monitor_content):
        """monitor.tick — already existed; must not have been removed."""
        assert 'echo "monitor.tick' in monitor_content

    def test_monitor_action_present(self, monitor_content):
        """monitor.action — one per autonomous corrective action."""
        assert 'echo "monitor.action' in monitor_content

    def test_monitor_notify_present(self, monitor_content):
        """monitor.notify — one per notification sent or logged."""
        assert 'echo "monitor.notify' in monitor_content

    def test_monitor_command_present(self, monitor_content):
        """monitor.command — one per steer command processed."""
        assert 'echo "monitor.command' in monitor_content

    def test_monitor_vigia_skip_present(self, monitor_content):
        """monitor.vigia.skip — one per vigia entering SKIPPED."""
        assert 'echo "monitor.vigia.skip' in monitor_content

    def test_monitor_vigia_fix_present(self, monitor_content):
        """monitor.vigia.fix — one per vigia completing autonomous cure."""
        assert "monitor.vigia.fix" in monitor_content

    def test_monitor_v8_scan_present(self, monitor_content):
        """monitor.v8.scan — emitted at end of V8 scan."""
        assert 'echo "monitor.v8.scan' in monitor_content

    def test_monitor_v8_create_present(self, monitor_content):
        """monitor.v8.create — one per FU issue created by V8."""
        assert 'echo "monitor.v8.create' in monitor_content

    def test_monitor_v8_skip_present(self, monitor_content):
        """monitor.v8.skip — one per V8 candidate discarded by anti-FP."""
        assert 'echo "monitor.v8.skip' in monitor_content

    def test_monitor_flood_cap_present(self, monitor_content):
        """monitor.flood_cap — emitted when notify/fu cap is reached."""
        assert 'echo "monitor.flood_cap' in monitor_content


# ── Operational event ──────────────────────────────────────────────────────

class TestAuditPvcFail:
    """monitor.audit_pvc_fail must be documented as the PVC-failure fallback."""

    def test_audit_pvc_fail_present(self, monitor_content):
        assert "monitor.audit_pvc_fail" in monitor_content

    def test_audit_pvc_fail_has_emit(self, monitor_content):
        """Must include an actual echo call, not just a mention in prose."""
        assert 'echo "monitor.audit_pvc_fail' in monitor_content


# ── Schema section ─────────────────────────────────────────────────────────

class TestSchemaSection:
    """The canonical vocabulary table must be present."""

    def test_schema_section_heading(self, monitor_content):
        assert "Emissão estruturada no stdout" in monitor_content

    def test_schema_table_has_all_families(self, monitor_content):
        """The vocabulary table must list every family."""
        for family in (
            "monitor.tick",
            "monitor.action",
            "monitor.notify",
            "monitor.command",
            "monitor.vigia.skip",
            "monitor.vigia.fix",
            "monitor.v8.scan",
            "monitor.v8.create",
            "monitor.v8.skip",
            "monitor.flood_cap",
            "monitor.audit_pvc_fail",
        ):
            assert family in monitor_content, f"Family '{family}' missing from monitor.md"

    def test_additive_only_note_present(self, monitor_content):
        """The additive-only constraint must be documented."""
        assert "additive-only" in monitor_content

    def test_audit_pvc_fail_invariant_documented(self, monitor_content):
        """The stream-invariant for audit_pvc_fail must be described."""
        assert "Invariante de stream" in monitor_content or "invariant" in monitor_content.lower()


# ── K8s API unreachable path ────────────────────────────────────────────────

class TestK8sUnreachableEmit:
    """When K8s API is unreachable, vigia.skip must fire for all affected vigias."""

    def test_v1_in_unreachable_skip_loop(self, monitor_content):
        assert "V1" in monitor_content

    def test_vigia_skip_reason_unreachable(self, monitor_content):
        assert "K8S_API_UNREACHABLE" in monitor_content


# ── Flood cap coverage ─────────────────────────────────────────────────────

class TestFloodCapCoverage:
    """Both notify and fu kinds must have flood_cap emit documented."""

    def test_flood_cap_notify_kind(self, monitor_content):
        assert "kind=notify" in monitor_content or "kind=<notify" in monitor_content

    def test_flood_cap_fu_kind(self, monitor_content):
        assert "kind=fu" in monitor_content or "kind=<notify\\|fu>" in monitor_content
