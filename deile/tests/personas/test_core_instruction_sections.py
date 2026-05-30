"""Regression tests for the content of core persona instruction files (Issue #423).

These tests snapshot the required sections of the DEILE core system prompt so
that accidental deletions or regressions are caught immediately. They do NOT
test LLM behaviour — they test that the instruction files contain the expected
textual markers that the model needs to auto-save user preferences correctly.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Path to the persona instruction files relative to the repo root.
_INSTRUCTIONS_DIR = (
    Path(__file__).parent.parent.parent  # deile/
    / "personas"
    / "instructions"
)
_CORE_MD = _INSTRUCTIONS_DIR / "core" / "DEILE.md"
_FALLBACK_MD = _INSTRUCTIONS_DIR / "fallback.md"


@pytest.fixture(scope="module")
def core_md_content() -> str:
    assert _CORE_MD.exists(), f"Core DEILE.md not found at {_CORE_MD}"
    return _CORE_MD.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def fallback_md_content() -> str:
    assert _FALLBACK_MD.exists(), f"fallback.md not found at {_FALLBACK_MD}"
    return _FALLBACK_MD.read_text(encoding="utf-8")


# ── Section presence: REGRA #15 ───────────────────────────────────────────

class TestPreferencesSection:
    """Snapshot checks for the 'Preferências do Usuário' block (REGRA #15)."""

    def test_section_heading_present(self, core_md_content):
        assert "Preferências do Usuário" in core_md_content

    def test_regra_number_present(self, core_md_content):
        assert "REGRA #15" in core_md_content

    def test_trigger_sempre_nunca(self, core_md_content):
        """Absolute-directive triggers must be listed."""
        assert "SEMPRE" in core_md_content
        assert "NUNCA" in core_md_content

    def test_trigger_de_agora_em_diante(self, core_md_content):
        assert "de agora em diante" in core_md_content

    def test_antiflood_rule_no_one_time_requests(self, core_md_content):
        """Anti-flood rule must distinguish one-shot vs persistent directives."""
        assert "pontuais" in core_md_content

    def test_antiflood_max_one_save_per_turn(self, core_md_content):
        """Max-1-save-per-turn limit must be documented."""
        assert "1 auto-save por turno" in core_md_content

    def test_antiflood_no_task_content_preferences(self, core_md_content):
        """Must prohibit saving task-content preferences (only operating mode)."""
        assert "modo de operar" in core_md_content

    def test_key_namespace_response(self, core_md_content):
        assert "response.language" in core_md_content

    def test_key_namespace_tools(self, core_md_content):
        assert "tools.prefer" in core_md_content

    def test_key_namespace_subagents(self, core_md_content):
        assert "subagents.mode" in core_md_content

    def test_no_generic_keys(self, core_md_content):
        """Must warn against generic key names."""
        assert "note_1" in core_md_content or "pref_2" in core_md_content

    def test_transparency_mention(self, core_md_content):
        """Must require a transparency line when auto-saving."""
        assert "forget_preference" in core_md_content

    def test_remember_preference_tool_named(self, core_md_content):
        assert "remember_preference" in core_md_content

    def test_section_within_line_budget(self, core_md_content):
        """The preferences section must not exceed 80 lines."""
        start_marker = "## 🧠 Preferências do Usuário"
        end_marker = "\n---\n"
        start = core_md_content.find(start_marker)
        assert start != -1, "Section start marker not found"
        # Find the next horizontal rule after the section start
        end = core_md_content.find(end_marker, start)
        if end == -1:
            # If no trailing separator, measure to the next ## heading
            end = core_md_content.find("\n## ", start + 1)
        section_text = core_md_content[start:end] if end != -1 else core_md_content[start:]
        line_count = section_text.count("\n")
        assert line_count <= 80, (
            f"Preferences section is {line_count} lines — must be ≤ 80"
        )


# ── Fallback reference ─────────────────────────────────────────────────────

class TestFallbackReference:
    """Fallback persona must reference the core rule."""

    def test_fallback_mentions_preferences(self, fallback_md_content):
        assert "Preferências" in fallback_md_content

    def test_fallback_points_to_core(self, fallback_md_content):
        """Must point reader to the full rule in core/DEILE.md."""
        assert "core/DEILE.md" in fallback_md_content or "REGRA #15" in fallback_md_content

    def test_fallback_mentions_remember_preference(self, fallback_md_content):
        assert "remember_preference" in fallback_md_content

    def test_fallback_mentions_antiflood(self, fallback_md_content):
        assert "anti-flood" in fallback_md_content.lower()
