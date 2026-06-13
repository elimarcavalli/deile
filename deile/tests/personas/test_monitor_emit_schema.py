"""Emit-schema contract test (post deterministic-tick refactor, 2026-06).

The 11 canonical ``monitor.*`` event families consumed by #436 (ACTIVITY widget)
and #440 (audit parser) are now produced in TWO surfaces:

* **Phase A (deterministic Python)** — ``infra/k8s/monitor_core.py`` /
  ``monitor_vigias.py`` / ``monitor_tick.py`` emit tick / action / notify /
  command / vigia.skip / vigia.fix / flood_cap(notify) / audit_pvc_fail and the
  quiet-tick ``v8.scan``.
* **Phase B (persona)** — ``deile/personas/instructions/monitor.md`` emits the
  follow-up families ``v8.create`` / ``v8.skip`` / ``v8.scan`` / ``flood_cap(fu)``
  and may emit ``notify``.

These tests pin the STABLE schema against whichever surface actually produces
each family, plus the regression that V1 no longer shells out to the impossible
interactive ``claude auth login``.
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent.parent          # deile/
_REPO = _ROOT.parent
_INFRA = _REPO / "infra" / "k8s"

_MONITOR_MD = _ROOT / "personas" / "instructions" / "monitor.md"
_CORE = _INFRA / "monitor_core.py"
_VIGIAS = _INFRA / "monitor_vigias.py"
_TICK = _INFRA / "monitor_tick.py"


@pytest.fixture(scope="module")
def persona() -> str:
    assert _MONITOR_MD.exists()
    return _MONITOR_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def phase_a() -> str:
    """Concatenated Phase-A Python source (the real producers)."""
    return "\n".join(p.read_text(encoding="utf-8") for p in (_CORE, _VIGIAS, _TICK))


# ── The canonical vocabulary table must document every family (shared contract) ──

class TestSchemaVocabularyTable:
    def test_schema_heading_present(self, persona):
        assert "Emissão estruturada no stdout" in persona

    @pytest.mark.parametrize("family", [
        "monitor.tick", "monitor.action", "monitor.notify", "monitor.command",
        "monitor.vigia.skip", "monitor.vigia.fix", "monitor.v8.scan",
        "monitor.v8.create", "monitor.v8.skip", "monitor.flood_cap",
        "monitor.audit_pvc_fail",
    ])
    def test_family_documented(self, persona, family):
        assert family in persona, f"family {family} missing from monitor.md schema table"

    def test_additive_only_note(self, persona):
        assert "additive-only" in persona

    def test_stream_invariant_documented(self, persona):
        assert "Invariante de stream" in persona

    def test_coding_rules_block(self, persona):
        assert "Regras de codificação da linha" in persona

    def test_no_secret_rule(self, persona):
        assert "Sem segredos" in persona


# ── Phase B (persona) emits the follow-up families via _emit ────────────────

class TestPhaseBEmits:
    @pytest.mark.parametrize("family", [
        "monitor.v8.create", "monitor.v8.skip", "monitor.v8.scan",
        "monitor.flood_cap", "monitor.notify",
    ])
    def test_phase_b_emit_point(self, persona, family):
        assert f'_emit "{family}' in persona, f"persona must emit {family} via _emit"

    def test_emit_helper_defined(self, persona):
        assert "_emit() {" in persona

    def test_emit_truncates_500(self, persona):
        assert '"${line:0:500}"' in persona

    def test_emit_strips_control_chars(self, persona):
        for ctrl in ("$'\\n'", "$'\\r'", "$'\\t'"):
            assert ctrl in persona

    def test_emit_stdout_first(self, persona):
        body = re.search(r"_emit\(\)\s*\{([\s\S]+?)^\}", persona, re.MULTILINE)
        assert body
        echo_idx = body.group(1).find('echo "$line"')
        printf_idx = body.group(1).find("printf")
        assert echo_idx != -1 and printf_idx != -1 and echo_idx < printf_idx

    def test_emit_pvc_fallback(self, persona):
        body = re.search(r"_emit\(\)\s*\{([\s\S]+?)^\}", persona, re.MULTILINE).group(1)
        assert "PVC_FAIL_EMITTED" in body and "monitor.audit_pvc_fail" in body

    def test_v8_skip_reason_enum_documented(self, persona):
        for reason in ("bot_author", "code_block", "already_tracked",
                       "fingerprint_seen", "daily_cap", "per_tick_cap"):
            assert reason in persona

    def test_flood_cap_fu_kind(self, persona):
        assert "kind=fu" in persona


# ── Phase A (Python) emits the operational families in canonical format ─────

class TestPhaseAEmits:
    def test_tick_emit(self, phase_a):
        assert "monitor.tick #" in phase_a and "done in" in phase_a

    def test_action_carries_v_token(self, phase_a):
        assert "monitor.action V=V" in phase_a

    def test_vigia_skip_carries_v_token(self, phase_a):
        # Source emits ``V={v}`` where v ∈ {V1,V2,V6,V7}; the V= field is the
        # contract. The concrete ``V=V1`` runtime value is asserted behaviorally
        # in test_monitor_tick.test_kube_unreachable_skips_with_v_token.
        assert "monitor.vigia.skip V=" in phase_a

    def test_vigia_fix_carries_v_token(self, phase_a):
        assert "monitor.vigia.fix V=V" in phase_a

    def test_command_uses_from_kind_not_cmd(self, phase_a):
        assert "monitor.command from=" in phase_a
        assert not re.search(r'monitor\.command [^"\n]*\bcmd=', phase_a)

    def test_command_unknown_branch(self, phase_a):
        assert re.search(r"monitor\.command [^\"\n]*kind=unknown[^\"\n]*ok=false", phase_a)

    def test_notify_has_ok_field(self, phase_a):
        # The emit f-string is wrapped across source lines; allow it (DOTALL).
        assert re.search(r"monitor\.notify .{0,200}ok=", phase_a, re.DOTALL)

    def test_flood_cap_notify_kind(self, phase_a):
        assert "monitor.flood_cap kind=notify" in phase_a

    def test_audit_pvc_fail_canonical(self, phase_a):
        assert re.search(
            r"monitor\.audit_pvc_fail reason='write failed' .{0,80}errno=.{0,40}tick=#",
            phase_a, re.DOTALL,
        )

    def test_k8s_unreachable_skip(self, phase_a):
        assert "K8S_API_UNREACHABLE" in phase_a


# ── Regression: the interactive OAuth login is GONE ─────────────────────────

class TestNoInteractiveOAuth:
    def test_persona_has_no_claude_auth_login(self, persona):
        assert "claude auth login" not in persona, (
            "the interactive `claude auth login` (impossible headless) must be "
            "removed; auth migrated to setup-token (issue #603)"
        )

    def test_phase_a_has_no_headless_refresh(self, phase_a):
        # Issue #603: auth migrou para o token de ~1 ano (setup-token,
        # CLAUDE_CODE_OAUTH_TOKEN via Secret). Não há mais refresh headless —
        # o símbolo do módulo removido NÃO pode reaparecer no Phase A.
        assert "try_refresh_claude_credentials" not in phase_a
        assert "_claude_creds_refresh" not in phase_a
        # E a remediação continua sendo um HINT (notificação), nunca um exec
        # de login interativo dentro do pod.
        assert not re.search(r"exec\b[^\n]*auth login", phase_a)


# ── Prompt-injection guard for Phase B (untrusted forge snippets) ───────────

class TestUntrustedInputGuard:
    def test_persona_documents_untrusted_input(self, persona):
        low = persona.lower()
        assert "não-confiável" in low or "nao-confiavel" in low or "injection" in low, (
            "persona must warn that fu_candidate snippets are untrusted forge text"
        )

    def test_persona_forbids_acting_on_snippet_instructions(self, persona):
        low = persona.lower()
        assert "dado a ser classificado" in low or "dado nao-confiavel" in low or "dado não-confiável" in low

    def test_persona_records_fingerprint_on_terminal_skip(self, persona):
        # FP / already-tracked skips must record the fingerprint so Phase A does
        # not re-escalate the same comment to the LLM every tick.
        assert "grave o fingerprint" in persona.lower() or "idempotência" in persona.lower()
