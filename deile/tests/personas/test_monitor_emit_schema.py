"""AC1 / AC2 / AC15 for issue #439 — monitor.md must define the `_emit` helper
and contain at least one emit point for each of the 10 canonical event families
+ the audit_pvc_fail operational event, all under the canonical schema
(V=V<n> for vigia-originated lines, from=/kind= for commands, closed enum for
v8.skip reasons).

These tests verify the STABLE CONTRACT consumed by #436 (ACTIVITY widget) and
#440 (monitor-audit parser).  They do NOT test LLM behaviour — they test that
the persona instruction file documents every required family so the model knows
to emit it.
"""
from __future__ import annotations

import re
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


# ── 10 canonical families + 1 operational (AC1) ─────────────────────────────

class TestCanonicalEmitFamilies:
    """Every family must have at least one concrete emit point in monitor.md.

    Spec (AC1): `monitor.tick` may use `echo` direct (passo 5.5 unchanged);
    `monitor.audit_pvc_fail` is the fallback inside `_emit` itself and may be a
    literal `echo`. Every other family must use the `_emit "monitor.<family>`
    helper so the call duplicates to stdout + PVC.
    """

    def test_monitor_tick_present(self, monitor_content):
        """monitor.tick — echo direct in passo 5.5 (preserved)."""
        assert 'echo "monitor.tick' in monitor_content

    def test_monitor_action_present(self, monitor_content):
        assert '_emit "monitor.action' in monitor_content

    def test_monitor_notify_present(self, monitor_content):
        assert '_emit "monitor.notify' in monitor_content

    def test_monitor_command_present(self, monitor_content):
        assert '_emit "monitor.command' in monitor_content

    def test_monitor_vigia_skip_present(self, monitor_content):
        assert '_emit "monitor.vigia.skip' in monitor_content

    def test_monitor_vigia_fix_present(self, monitor_content):
        assert '_emit "monitor.vigia.fix' in monitor_content

    def test_monitor_v8_scan_present(self, monitor_content):
        assert '_emit "monitor.v8.scan' in monitor_content

    def test_monitor_v8_create_present(self, monitor_content):
        assert '_emit "monitor.v8.create' in monitor_content

    def test_monitor_v8_skip_present(self, monitor_content):
        assert '_emit "monitor.v8.skip' in monitor_content

    def test_monitor_flood_cap_present(self, monitor_content):
        assert '_emit "monitor.flood_cap' in monitor_content


# ── Operational event ──────────────────────────────────────────────────────

class TestAuditPvcFail:
    """monitor.audit_pvc_fail must be documented as the PVC-failure fallback,
    living inside the `_emit` helper itself (one literal `echo` is enough)."""

    def test_audit_pvc_fail_present(self, monitor_content):
        assert "monitor.audit_pvc_fail" in monitor_content

    def test_audit_pvc_fail_has_emit(self, monitor_content):
        """Must include an actual echo call inside `_emit`, not just a mention in prose."""
        assert 'echo "monitor.audit_pvc_fail' in monitor_content

    def test_audit_pvc_fail_canonical_fields(self, monitor_content):
        """Spec requires `reason='write failed' errno=<código> tick=#<n>`."""
        pattern = re.compile(
            r"echo \"monitor\.audit_pvc_fail reason='write failed' errno=\$\{_?errno\} tick=#\$\{TICK_N",
            re.IGNORECASE,
        )
        assert pattern.search(monitor_content), (
            "monitor.audit_pvc_fail must follow canonical schema "
            "`reason='write failed' errno=<código> tick=#<n>`"
        )


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
        assert "additive-only" in monitor_content

    def test_audit_pvc_fail_invariant_documented(self, monitor_content):
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
        assert "kind=notify" in monitor_content

    def test_flood_cap_fu_kind(self, monitor_content):
        assert "kind=fu" in monitor_content


# ── AC2 — `_emit` helper defined & widely used ─────────────────────────────

class TestEmitHelper:
    """AC2: `_emit` must be defined with the spec body AND used in ≥10 places."""

    def test_emit_defined(self, monitor_content):
        """`_emit() {` definition must appear in the body of monitor.md."""
        assert "_emit() {" in monitor_content, (
            "monitor.md must define the `_emit()` bash helper per spec §AC2"
        )

    def test_emit_truncates_to_500_chars(self, monitor_content):
        """Spec rule 4: linha total máxima 500 chars (cortada por `_emit`)."""
        assert '"${line:0:500}"' in monitor_content, (
            "_emit must truncate the line to 500 chars (rule 4)"
        )

    def test_emit_strips_control_chars(self, monitor_content):
        """Spec rule 3: strip de \\n, \\r, \\t (single-line invariant)."""
        for ctrl in ("$'\\n'", "$'\\r'", "$'\\t'"):
            assert ctrl in monitor_content, (
                f"_emit must strip control char {ctrl!r} from values (rule 3)"
            )

    def test_emit_writes_stdout_first(self, monitor_content):
        """Spec rule 6: PRIMEIRO echo no stdout, DEPOIS printf no PVC."""
        # The function body must echo before printf-to-PVC; check structural order
        emit_body = re.search(
            r"_emit\(\)\s*\{([\s\S]+?)^\}",
            monitor_content,
            re.MULTILINE,
        )
        assert emit_body, "could not locate `_emit()` body"
        body = emit_body.group(1)
        echo_idx = body.find('echo "$line"')
        printf_idx = body.find("printf")
        assert echo_idx != -1 and printf_idx != -1, (
            "_emit body must contain both `echo \"$line\"` and `printf`"
        )
        assert echo_idx < printf_idx, (
            "stdout-first invariant: `echo` must run before `printf` to PVC (rule 6)"
        )

    def test_emit_pvc_fallback_emits_audit_pvc_fail(self, monitor_content):
        """Spec rule 6: on PVC write failure, emit `monitor.audit_pvc_fail` once per tick."""
        emit_body = re.search(
            r"_emit\(\)\s*\{([\s\S]+?)^\}",
            monitor_content,
            re.MULTILINE,
        )
        assert emit_body
        body = emit_body.group(1)
        assert "PVC_FAIL_EMITTED" in body, (
            "_emit must guard the audit_pvc_fail echo with PVC_FAIL_EMITTED flag"
        )
        assert "monitor.audit_pvc_fail" in body, (
            "_emit must emit monitor.audit_pvc_fail on PVC write failure"
        )

    def test_emit_used_at_least_10_times(self, monitor_content):
        """AC2: `grep -c '_emit ' monitor.md` ≥ 10."""
        count = len(re.findall(r"\b_emit ", monitor_content))
        assert count >= 10, (
            f"_emit must be invoked at ≥10 emit points (AC2); found {count}"
        )

    def test_tick_flags_reset_in_step_1(self, monitor_content):
        """Spec §Helper bash: PVC_FAIL_EMITTED / FLOOD_CAP_EMITTED_* reset at start of tick."""
        for flag in ("PVC_FAIL_EMITTED=0", "FLOOD_CAP_EMITTED_NOTIFY=0", "FLOOD_CAP_EMITTED_FU=0"):
            assert flag in monitor_content, (
                f"per-tick flag reset `{flag}` must appear (step 1 of tick loop)"
            )


# ── AC15 — V=V<n> token enforcement ────────────────────────────────────────

class TestVigiaTokenConvention:
    """AC15: every `monitor.action` / `monitor.vigia.{skip,fix}` emit must carry V=V<n>."""

    _VIGIA_FAMILY_RE = re.compile(
        r'^[^#\n]*_emit "(monitor\.(?:action|vigia\.(?:skip|fix))) ([^"]*)"',
        re.MULTILINE,
    )

    def test_every_vigia_originated_emit_carries_v_token(self, monitor_content):
        """Each concrete `_emit "monitor.<vigia-family> ..."` line must include V=V<n>."""
        offenders: list[str] = []
        for match in self._VIGIA_FAMILY_RE.finditer(monitor_content):
            family, payload = match.group(1), match.group(2)
            # Accept either:
            #   `V=V<digit>` literal (e.g. V=V1)
            #   `V=V<n>` placeholder in patterns / examples
            #   `V=${var}` bash-substituted vigia loop var (resolves to V1/V2/... at runtime)
            if not re.search(r"V=(V[1-9]|V<n>|V<\$|\$\{)", payload):
                offenders.append(f"{family}: {payload[:80]}")
        assert not offenders, (
            "monitor.action / monitor.vigia.* emit points must include `V=V<n>` (AC15). "
            "Offenders:\n" + "\n".join(offenders)
        )

    def test_no_legacy_vigia_eq_token(self, monitor_content):
        """The legacy `vigia=V<n>` token must be gone in active emit lines (AC15).

        We allow it only inside the canonical-table example column (rendered with
        the `vigia=` literal as a *forbidden* form) — but no `_emit` call should
        produce a `vigia=V<n>` field.
        """
        offenders = re.findall(r'_emit "[^"]*\bvigia=V[1-9][^"]*"', monitor_content)
        assert not offenders, (
            "Legacy `vigia=V<n>` token must not appear in emit lines; use `V=V<n>` "
            "(AC15). Offenders:\n" + "\n".join(offenders)
        )


# ── AC13 — monitor.command schema ───────────────────────────────────────────

class TestCommandSchema:
    """AC13 / §Lista fechada de kind=: monitor.command uses `from=`/`kind=` not `cmd=`."""

    def test_command_uses_from_kind_not_cmd(self, monitor_content):
        """Spec: `monitor.command from=<bot|auto> kind=<...> ok=...` — never `cmd=`."""
        offenders = re.findall(
            r'_emit "monitor\.command [^"]*\bcmd=', monitor_content
        )
        assert not offenders, (
            "`monitor.command` must use `from=...` + `kind=...` (spec §Lista fechada de kind=), "
            "not the legacy `cmd=` field. Offenders:\n" + "\n".join(offenders)
        )

    def test_command_has_from_field(self, monitor_content):
        assert re.search(r'_emit "monitor\.command from=', monitor_content), (
            "monitor.command emit must start with `from=<bot|auto>`"
        )

    def test_command_unknown_branch_documented(self, monitor_content):
        """AC13: malformed commands → kind=unknown with ok=false."""
        assert re.search(
            r'_emit "monitor\.command [^"]*kind=unknown[^"]*ok=false',
            monitor_content,
        ), "Spec §Lista fechada / AC13 requires a `kind=unknown ok=false` emit example"


# ── monitor.v8.skip reason enum ─────────────────────────────────────────────

class TestV8SkipReasonEnum:
    """Spec §Lista fechada de reason= for monitor.v8.skip:
    bot_author | code_block | already_tracked | fingerprint_seen | daily_cap | per_tick_cap.
    `similar_title` was removed; `author_bot` was renamed to `bot_author`.
    """

    def test_canonical_reasons_documented(self, monitor_content):
        for reason in (
            "bot_author",
            "code_block",
            "already_tracked",
            "fingerprint_seen",
            "daily_cap",
            "per_tick_cap",
        ):
            assert reason in monitor_content, (
                f"v8.skip reason `{reason}` must be documented in monitor.md"
            )

    def test_legacy_author_bot_removed(self, monitor_content):
        """`author_bot` (legacy) must not appear as the documented v8.skip reason
        — the spec renamed it to `bot_author`."""
        # Allow the substring inside other words (e.g. in prose), but not as
        # an emit-field value `reason=author_bot`.
        offenders = re.findall(r"reason=author_bot\b", monitor_content)
        assert not offenders, (
            "Legacy `reason=author_bot` must be replaced with `reason=bot_author`"
        )

    def test_legacy_similar_title_removed(self, monitor_content):
        offenders = re.findall(r"reason=similar_title\b", monitor_content)
        assert not offenders, (
            "Legacy `reason=similar_title` must be removed (not in spec enum)"
        )


# ── monitor.notify ok= field ────────────────────────────────────────────────

class TestNotifyHasOkField:
    def test_notify_emit_has_ok(self, monitor_content):
        """Spec table + AC8: monitor.notify emit must include `ok=`."""
        match = re.search(r'_emit "monitor\.notify [^"]*"', monitor_content)
        assert match, "monitor.notify _emit line not found"
        assert "ok=" in match.group(0), (
            "monitor.notify emit must include `ok=<true|false>` (spec §Vocabulário canônico)"
        )


# ── V1 OAuth — Rule 5 (no secret leak) ──────────────────────────────────────

class TestV1SecretSuppression:
    """AC4 / spec rule 5: V1 OAuth must redirect stdout/stderr away."""

    def test_v1_oauth_redirects_stdout(self, monitor_content):
        """The OAuth login command must include `>/dev/null 2>&1` to suppress token leak."""
        # Locate the V1 section roughly; we look for `claude auth login` and confirm a
        # redirect is in scope.
        match = re.search(
            r"claude auth login[^\n]*>/dev/null 2>&1",
            monitor_content,
        )
        assert match, (
            "V1 OAuth renew must redirect stdout/stderr with `>/dev/null 2>&1` "
            "(spec rule 5 / AC4: never echo token)"
        )

    def test_rule5_documented_in_schema_section(self, monitor_content):
        """The 8 schema-line rules must be documented (rule 5 = no secret leak)."""
        assert "Sem segredos" in monitor_content, (
            "Spec rule 5 (`Sem segredos`) must be written into the schema section"
        )

    def test_schema_rules_block_present(self, monitor_content):
        """The block of 8 rules ('Regras de codificação da linha') must be in the persona."""
        assert "Regras de codificação da linha" in monitor_content, (
            "The 8 schema-line rules must appear under a `Regras de codificação` heading"
        )
