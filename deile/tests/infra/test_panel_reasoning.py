"""Panel reasoning column: data-layer validation + matrix render/picker.

Covers ``_panel_data`` set/clear validation (no kubectl needed — validation
runs before argv) and the ``DispatchMatrixView`` Reasoning column (col 5):
render, contextual picker options, demo apply.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_INFRA_K8S = Path(__file__).resolve().parents[3] / "infra" / "k8s"
if str(_INFRA_K8S) not in sys.path:
    sys.path.insert(0, str(_INFRA_K8S))


@pytest.mark.ui
class TestClaudeWorkerEffortCoercion:
    """claude-worker traduz o vocabulário Claude Code para o que `claude --effort` aceita.

    Verificado empiricamente: `claude --effort` só aceita low|medium|high|xhigh|max.
    ``auto``/``ultracode`` REJEITAM o CLI (saída com erro antes de qualquer API),
    então a dispatch falharia 100%. O coercion traduz no boundary.
    """

    def test_valid_set_excludes_auto_and_ultracode(self):
        import claude_worker_server as cw
        assert cw._VALID_CLAUDE_EFFORTS == {"low", "medium", "high", "xhigh", "max"}
        assert "auto" not in cw._VALID_CLAUDE_EFFORTS
        assert "ultracode" not in cw._VALID_CLAUDE_EFFORTS

    def test_coerce_auto_omits(self):
        import claude_worker_server as cw
        assert cw._coerce_claude_effort("auto") is None
        assert cw._coerce_claude_effort(None) is None
        assert cw._coerce_claude_effort("") is None

    def test_coerce_ultracode_to_xhigh(self):
        import claude_worker_server as cw

        # ultracode = xhigh + "workflow" no prompt (modo interativo);
        # em -p replicamos só o esforço com xhigh.
        assert cw._coerce_claude_effort("ultracode") == "xhigh"

    def test_coerce_valid_passthrough_and_normalizes(self):
        import claude_worker_server as cw
        for lvl in ("low", "medium", "high", "xhigh", "max"):
            assert cw._coerce_claude_effort(lvl) == lvl
        assert cw._coerce_claude_effort("  HIGH ") == "high"

    def test_coerce_unknown_omits(self):
        import claude_worker_server as cw

        # off/none/minimal (other providers' vocab) or junk → omit, never passed to CLI
        for bad in ("off", "none", "minimal", "bogus"):
            assert cw._coerce_claude_effort(bad) is None


@pytest.mark.ui
class TestClaudeWorkerUltracode:
    """Ultracode = xhigh (no --effort) + keyword "workflow" no prompt.

    O ``--effort`` cobre o xhigh; a segunda metade do preset (opt-in no Workflow
    tool) não tem flag de CLI — o binário detecta a palavra ``workflow`` no
    prompt. ``_is_ultracode`` decide quando prefixar ``_ULTRACODE_PREAMBLE``.
    """

    def test_is_ultracode_true_only_for_ultracode(self):
        import claude_worker_server as cw
        assert cw._is_ultracode("ultracode") is True
        assert cw._is_ultracode("  Ultracode ") is True
        assert cw._is_ultracode("ULTRACODE") is True

    def test_is_ultracode_false_for_other_levels(self):
        import claude_worker_server as cw
        for lvl in ("xhigh", "max", "high", "auto", None, "", "workflow"):
            assert cw._is_ultracode(lvl) is False

    def test_ultracode_coerces_effort_to_xhigh(self):
        # A primeira metade do preset: ultracode → --effort xhigh (Sonnet 4.6
        # está na allowlist xhigh do binário claude; funciona nativamente).
        import claude_worker_server as cw
        assert cw._coerce_claude_effort("ultracode") == "xhigh"

    def test_preamble_carries_workflow_keyword(self):
        # O keyword "workflow" no prompt é o que o CLI usa pra opt-in no
        # Workflow tool — sem ele, ultracode seria só xhigh.
        import claude_worker_server as cw
        assert "workflow" in cw._ULTRACODE_PREAMBLE.lower()
        # Termina com separador para não colar no preamble do stage.
        assert cw._ULTRACODE_PREAMBLE.endswith("---\n\n")


@pytest.mark.ui
class TestPanelDataReasoning:
    def test_entry_has_reasoning_field(self):
        from _panel_data import StageDispatchEntry
        e = StageDispatchEntry("implement", "deile-worker",
                               "anthropic:claude-sonnet-4-6", "env",
                               reasoning="high")
        assert e.reasoning == "high"

    def test_set_stage_reasoning_rejects_bad_level(self):
        from _panel_data import set_stage_reasoning
        ok, msg = set_stage_reasoning("implement", "bogus")
        assert ok is False
        assert "inválido" in msg

    def test_set_stage_reasoning_rejects_bad_stage(self):
        from _panel_data import set_stage_reasoning
        ok, msg = set_stage_reasoning("nope", "high")
        assert ok is False

    def test_set_global_reasoning_rejects_bad_level(self):
        from _panel_data import set_global_reasoning
        ok, msg = set_global_reasoning("garbage")
        assert ok is False

    def test_reasoning_env_var_mapping_complete(self):
        from _panel_data import _STAGE_REASONING_ENV_VARS
        stages = {s for s, _ in _STAGE_REASONING_ENV_VARS}
        assert stages == {"classify", "refine", "implement", "pr_review", "follow_ups"}
        for _, env in _STAGE_REASONING_ENV_VARS:
            assert env.startswith("DEILE_PIPELINE_REASONING_")


@pytest.mark.ui
class TestDispatchMatrixReasoningColumn:
    def test_render_demo_does_not_crash(self):
        import _panel as P
        from rich.console import Console
        v = P.DispatchMatrixView(data=None)
        import io
        c = Console(file=io.StringIO(), width=200)
        c.print(v.render(None))  # must not raise

    def test_picker_options_contextual(self):
        import _panel as P
        v = P.DispatchMatrixView(data=None)
        claude = v._reasoning_picker_options(worker="claude-worker", model="anthropic:claude-opus-4-8")
        assert claude[1:] == ["low", "medium", "high", "xhigh", "max", "ultracode", "auto"]
        openai = v._reasoning_picker_options(worker="deile-worker", model="openai:gpt-5.4")
        assert "none" in openai and "xhigh" in openai
        gemini = v._reasoning_picker_options(worker="deile-worker", model="gemini:gemini-2.5-pro")
        assert "off" in gemini
        deepseek = v._reasoning_picker_options(worker="deile-worker", model="deepseek:deepseek-v4-pro")
        assert deepseek[1:] == ["off", "high", "max", "auto"]

    def test_nav_reaches_col_5(self):
        import _panel as P
        v = P.DispatchMatrixView(data=None)
        v.cursor_col = 0
        for _ in range(10):
            v._handle_key_safe("RIGHT", None)
        assert v.cursor_col == 5  # clamped at reasoning column

    def test_open_reasoning_picker_demo(self):
        import _panel as P
        v = P.DispatchMatrixView(data=None)
        entries = v._entries()
        v._open_reasoning_picker(entries[0])
        assert v.mode is not None and v.mode[0] == "reasoning"

    def test_global_reasoning_picker_union(self):
        import _panel as P
        v = P.DispatchMatrixView(data=None)
        v._open_global_reasoning_picker()
        assert v.mode[0] == "global_reasoning"
        # union includes provider-specific levels
        opts = v.mode[2]
        assert "(clear override)" == opts[0]
        for lvl in ("ultracode", "none", "off", "max"):
            assert lvl in opts
