"""Renderer tests — feed a fake stream and assert the captured output."""

from __future__ import annotations

import io
from typing import AsyncIterator, List

import pytest
from rich.console import Console

from deile.core.models.stream_events import (ModelUsageSnapshot,
                                             StreamEventType,
                                             UnifiedStreamEvent)
from deile.ui.streaming_renderer import StreamingRenderer


async def _replay(events: List[UnifiedStreamEvent]) -> AsyncIterator[UnifiedStreamEvent]:
    for e in events:
        yield e


def _capture_console() -> Console:
    return Console(file=io.StringIO(), width=120, force_terminal=False, no_color=True)


@pytest.mark.asyncio
async def test_text_only_stream_aggregates_correctly():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello "),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="world"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=5, output_tokens=2),
        ),
    ]
    result = await renderer.render(_replay(events))
    assert "hello world" == result.full_text
    assert result.tool_invocations == 0
    assert result.error_message is None


@pytest.mark.asyncio
async def test_usage_footer_includes_model_when_present():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(
                input_tokens=10,
                output_tokens=3,
                cost_usd=0.0012,
                model="anthropic:claude-haiku-4-5",
            ),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "anthropic:claude-haiku-4-5" in output
    assert "$0.0012" in output


@pytest.mark.asyncio
async def test_usage_footer_omits_model_when_absent():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hi"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=10, output_tokens=3),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # No model emitted → no provider:model substring in footer.
    assert ":" not in output.split("hourglass")[-1].split("\n")[0]


@pytest.mark.asyncio
async def test_tool_use_lifecycle_renders_and_aggregates():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Let me check. "),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="bash_execute",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="bash_execute",
            arguments={"command": "ls"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="bash_execute",
            tool_status="success",
            tool_result_summary="2 files",
        ),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Done."),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=10, output_tokens=4),
        ),
    ]
    result = await renderer.render(_replay(events))
    assert result.tool_invocations == 1
    assert result.tool_failures == 0
    output = console.file.getvalue()
    # bash_execute aparece como "Bash" (display name amigável)
    assert "Bash" in output
    # Comando aparece bare, sem "command='...'" wrapping
    assert "ls" in output
    assert "command='ls'" not in output
    assert "2 files" in output
    # Marker ⎿ usado para a linha de resumo
    assert "⎿" in output


@pytest.mark.asyncio
async def test_interleaved_text_and_bash_preserves_visual_order():
    """Regressão: texto do modelo entre tools direct-print precisa aparecer
    NO LUGAR onde foi emitido (entre os tool blocks), não acumulado no fim.

    Antes do fix, ``_commit_direct_print_tools`` rodava ANTES do active_idx
    commit do laço principal — então o cabeçalho da bash ia para scrollback
    primeiro, e o texto precedente do modelo ficava preso na Live region
    até o fim do turno, aparecendo todo junto após as bash outputs.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Passo 1: criar arquivo."),
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_START, tool_call_id="t1", tool_name="bash_execute"),
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_END, tool_call_id="t1", tool_name="bash_execute",
                           arguments={"command": "echo hi"}),
        UnifiedStreamEvent(type=StreamEventType.TOOL_RESULT, tool_call_id="t1", tool_name="bash_execute",
                           tool_status="success", tool_result_summary="ok1"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Passo 2: validar."),
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_START, tool_call_id="t2", tool_name="bash_execute"),
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_END, tool_call_id="t2", tool_name="bash_execute",
                           arguments={"command": "cat x"}),
        UnifiedStreamEvent(type=StreamEventType.TOOL_RESULT, tool_call_id="t2", tool_name="bash_execute",
                           tool_status="success", tool_result_summary="ok2"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Pronto."),
        UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL,
                           usage=ModelUsageSnapshot(input_tokens=10, output_tokens=4)),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Ordem visual: cada marker tem que aparecer ANTES do próximo, na
    # mesma ordem que os eventos foram emitidos.
    markers = ["Passo 1", "echo hi", "ok1", "Passo 2", "cat x", "ok2", "Pronto"]
    positions = [output.index(m) for m in markers]
    assert positions == sorted(positions), (
        f"Ordem incorreta. Markers em ordem de aparecer: {sorted(zip(positions, markers))}"
    )


@pytest.mark.asyncio
async def test_bash_header_not_duplicated_when_block_commits_later():
    """Regressão: tool block com head_committed=True não pode ser re-renderizado
    pelo loop de commit do active_idx — duplicaria o cabeçalho na scrollback.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_START, tool_call_id="t1", tool_name="bash_execute"),
        UnifiedStreamEvent(type=StreamEventType.TOOL_USE_END, tool_call_id="t1", tool_name="bash_execute",
                           arguments={"command": "echo unique-marker-xyz"}),
        UnifiedStreamEvent(type=StreamEventType.TOOL_RESULT, tool_call_id="t1", tool_name="bash_execute",
                           tool_status="success", tool_result_summary="ok"),
        # Texto subsequente força o tool block para "committed" via active_idx.
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="depois"),
        UnifiedStreamEvent(type=StreamEventType.USAGE_FINAL,
                           usage=ModelUsageSnapshot(input_tokens=5, output_tokens=2)),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # O command único só pode aparecer UMA vez na saída.
    assert output.count("unique-marker-xyz") == 1


@pytest.mark.asyncio
async def test_bash_long_command_truncated_in_header():
    """Comando bash longo é truncado com '…' no cabeçalho."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    long_cmd = "find /Users/elimar.cavalli/dev -type f -not -path '*/node_modules/*' -name '*.py'"
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="bash_execute",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="bash_execute",
            arguments={"command": long_cmd, "working_directory": "/tmp"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="bash_execute",
            tool_status="success",
            tool_result_summary="291301 total",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # working_directory NÃO aparece (mostramos só o command primário)
    assert "working_directory" not in output
    # Truncamento usa '…' depois de 77 chars do command
    assert "…" in output
    # Summary com marker correto
    assert "291301 total" in output


@pytest.mark.asyncio
async def test_non_bash_tool_keeps_kwarg_format():
    """Tools sem primary_arg mapeado usam o formato `chave='valor'`."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="write_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="write_file",
            arguments={"path": "x.txt", "content": "hi"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="write_file",
            tool_status="success",
            tool_result_summary="ok",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # write_file não está em _TOOL_DISPLAY_NAME → mantém nome original
    assert "write_file" in output
    # kwarg format preservado para tools genéricas
    assert "path=" in output


@pytest.mark.asyncio
async def test_edit_file_renders_path_and_patch_count():
    """edit_file mostra `path, N patches` — não o Python repr da lista."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="ef1",
            tool_name="edit_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="ef1",
            tool_name="edit_file",
            arguments={
                "file_path": "deile/foo/bar.py",
                "patches": [
                    {"find": "x = 1", "replace": "x = 42"},
                    {"find": "y = 2", "replace": "y = 99"},
                    {"find": "z = 3", "replace": "z = 7"},
                ],
            },
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="ef1",
            tool_name="edit_file",
            tool_status="success",
            tool_result_summary="3 patches applied",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "edit_file" in output
    assert "deile/foo/bar.py" in output
    assert "3 patches" in output
    # The ugly Python repr of the list must NOT leak through.
    assert "'find':" not in output
    assert "[{'find" not in output


@pytest.mark.asyncio
async def test_edit_file_single_patch_uses_singular_label():
    """Plural correto: 1 patch (não 1 patches)."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="ef2",
            tool_name="edit_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="ef2",
            tool_name="edit_file",
            arguments={
                "file_path": "a.py",
                "patches": [{"find": "x", "replace": "y"}],
            },
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="ef2",
            tool_name="edit_file",
            tool_status="success",
            tool_result_summary="ok",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "1 patch" in output
    assert "1 patches" not in output


@pytest.mark.asyncio
async def test_edit_file_truncates_long_path():
    """Paths longos são truncados pela esquerda com elipse — `…/end/of/path.py`."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    long_path = "deile/" + "subdir/" * 12 + "module.py"
    assert len(long_path) > 60
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="ef3",
            tool_name="edit_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="ef3",
            tool_name="edit_file",
            arguments={
                "file_path": long_path,
                "patches": [{"find": "x", "replace": "y"}],
            },
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="ef3",
            tool_name="edit_file",
            tool_status="success",
            tool_result_summary="ok",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "module.py" in output  # filename always preserved
    assert "…" in output           # ellipsis present


@pytest.mark.asyncio
async def test_file_tools_render_primary_path_not_kwarg():
    """read_file/write_file/list_files/delete_file mostram só o path,
    sem o ruído ``file_path='...'`` nem o ``content='...'`` no header.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="r1", tool_name="read_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="r1", tool_name="read_file",
            arguments={"file_path": "deile/foo.py"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT, tool_call_id="r1", tool_name="read_file",
            tool_status="success", tool_result_summary="ok",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="w1", tool_name="write_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="w1", tool_name="write_file",
            arguments={"file_path": "deile/bar.py", "content": "print('hi-42')"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT, tool_call_id="w1", tool_name="write_file",
            tool_status="success", tool_result_summary="ok",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="l1", tool_name="list_files",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="l1", tool_name="list_files",
            arguments={"path": "deile/"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT, tool_call_id="l1", tool_name="list_files",
            tool_status="success", tool_result_summary="ok",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="d1", tool_name="delete_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="d1", tool_name="delete_file",
            arguments={"file_path": "deile/old.py", "force": True},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT, tool_call_id="d1", tool_name="delete_file",
            tool_status="success", tool_result_summary="ok",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # All paths present
    assert "deile/foo.py" in output
    assert "deile/bar.py" in output
    assert "deile/old.py" in output
    # NONE of the keyword-style noise should leak.
    assert "file_path=" not in output
    # content='...' is the worst offender — must be gone for write_file.
    assert "content=" not in output
    assert "force=" not in output


@pytest.mark.asyncio
async def test_python_execute_collapses_multiline_in_header():
    """python_execute(code='...') com newlines vira `... ⏎ ...` no header."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="p1", tool_name="python_execute",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="p1", tool_name="python_execute",
            arguments={"code": "x = 1\ny = 2\nprint(x + y)"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT, tool_call_id="p1", tool_name="python_execute",
            tool_status="success", tool_result_summary="exit 0 • 23ms",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Friendly display name + collapsed newlines
    assert "Python" in output
    assert "⏎" in output
    # The raw representation must NOT leak.
    assert "code=" not in output


@pytest.mark.asyncio
async def test_proactive_prefix_renders_visibly():
    """Tools proativas chegam como ``proactive:<tool>`` e devem ser visíveis
    no transcript com o mesmo formato `● name(args)`. Garante que o prefixo
    aparece para o usuário diferenciar do que a IA escolheu.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="pr1",
            tool_name="proactive:read_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="pr1",
            tool_name="proactive:read_file",
            arguments={"file_path": "context.py"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="pr1",
            tool_name="proactive:read_file",
            tool_status="success",
            tool_result_summary="42 bytes read",
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "proactive:read_file" in output
    assert "context.py" in output
    assert "42 bytes read" in output


@pytest.mark.asyncio
async def test_tool_running_state_is_visible_before_result():
    """O bloco da tool aparece em estado 'running…' enquanto executa.

    Garante que a tool NÃO executa silenciosa: o usuário vê o nome dela
    e os args no transcript antes do TOOL_RESULT chegar — ou seja, durante
    a janela em que a execução está acontecendo.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    # Sem TOOL_RESULT: simula o instante em que a tool ainda está executando.
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="r1",
            tool_name="edit_file",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="r1",
            tool_name="edit_file",
            arguments={
                "file_path": "live.py",
                "patches": [{"find": "a", "replace": "b"}],
            },
        ),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "edit_file" in output
    assert "live.py" in output
    assert "running" in output  # status visible while executing


@pytest.mark.asyncio
async def test_tool_failure_marks_error_status():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="bash_execute",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="bash_execute",
            arguments={},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="bash_execute",
            tool_status="error",
            tool_result_summary="exit 1",
        ),
    ]
    result = await renderer.render(_replay(events))
    assert result.tool_invocations == 1
    assert result.tool_failures == 1


@pytest.mark.asyncio
async def test_error_event_captured():
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="part "),
        UnifiedStreamEvent(
            type=StreamEventType.ERROR,
            error_envelope={"message": "boom", "error_type": "auth"},
        ),
    ]
    result = await renderer.render(_replay(events))
    assert result.error_message == "boom"


@pytest.mark.asyncio
async def test_validation_gate_text_renders_in_yellow_panel():
    """The renderer must visually distinguish validation_gate-sourced text."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="primary"),
        UnifiedStreamEvent(
            type=StreamEventType.TEXT_DELTA, text="ps note", source="validation_gate"
        ),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "primary" in output
    # Validation-gate panel renders the text within its own block — output present
    assert "ps note" in output
    assert "primary" in result.full_text


# ----------------------------------------------------------------------
# Markdown-aware streaming tests — guard rails for the regression where
# legacy_windows mode silently disabled Markdown rendering, leaving raw
# ``**bold**`` and ``# heading`` characters on the user's terminal.
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_partial_bold_run_renders_as_bold_when_completed_live():
    """``**tex`` + ``to**`` arriving as separate deltas must end up bold.

    The renderer must accumulate before parsing — never try to parse a
    delta in isolation. This is the heart of the streaming Markdown fix.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=True)
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello "),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="**tex"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="to**"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=" world"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    result = await renderer.render(_replay(events))
    # Internally accumulated string is the full markdown source — the
    # renderer hands this to rich.markdown.Markdown, which strips the
    # ``**`` markers in the rendered output.
    assert result.full_text == "hello **texto** world"
    output = console.file.getvalue()
    assert "texto" in output
    # The literal ``**`` markers must NOT survive into the rendered output
    # (markdown=True + complete bold run → Rich strips them).
    assert "**" not in output


@pytest.mark.asyncio
async def test_partial_bold_run_renders_as_bold_when_completed_legacy():
    """Same partial-bold scenario, but on the legacy (non-Live) path.

    Regression test for the original bug: with the previous code, the
    legacy path printed deltas raw (``console.print(event.text, end="")``),
    so ``**texto**`` survived literally on Windows-style terminals. The
    fix is to accumulate + render Markdown even without Live.
    """
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=True, markdown=True, refresh_per_second=30.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello "),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="**tex"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="to**"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=" world\n"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert result.full_text == "hello **texto** world\n"
    # Markdown was rendered → no literal ``**`` markers in output.
    assert "**" not in output
    assert "texto" in output


@pytest.mark.asyncio
async def test_unclosed_code_fence_is_held_back_until_close_legacy():
    """An open ``"```py"`` mid-stream must not leak fence chars to the
    terminal until the closing ``"```"`` arrives.

    This is the lookahead-buffering requirement: parsers that just print
    every delta corrupt code-block layout. We verify that no raw triple
    backtick appears in the output once the fence is properly closed and
    that the fenced content is rendered.
    """
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=True, markdown=True, refresh_per_second=30.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="See:\n\n```py\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="x = 1\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="y = 2\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="```\n"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Code content survives.
    assert "x = 1" in output
    assert "y = 2" in output
    # Fence markers are stripped by Markdown rendering — they should NOT
    # appear in the rendered output as literal backticks.
    assert "```" not in output


@pytest.mark.asyncio
async def test_partial_table_does_not_corrupt_final_layout_legacy():
    """A Markdown table arriving across many deltas must render correctly
    once complete (no half-rendered rows leaked between flushes)."""
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=True, markdown=True, refresh_per_second=30.0
    )
    pieces = [
        "| Col A | Col B |\n",
        "| --- | --- |\n",
        "| a1 ",
        "| b1 |\n",
        "| a2 | b2 |\n",
    ]
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text=p) for p in pieces
    ]
    events.append(
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        )
    )
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    # All cell contents present — table rendered as a whole, not byte-by-byte.
    for cell in ("Col A", "Col B", "a1", "b1", "a2", "b2"):
        assert cell in output
    # Accumulated source is intact.
    assert "| Col A | Col B |" in result.full_text


@pytest.mark.asyncio
async def test_refresh_per_second_is_configurable():
    """``refresh_per_second`` is wired through to the renderer and clamped
    to a sane minimum (>= 1Hz)."""
    console = _capture_console()
    r1 = StreamingRenderer(console=console, refresh_per_second=30.0)
    assert r1._refresh_hz == 30.0
    r2 = StreamingRenderer(console=console, refresh_per_second=0.0)
    # Clamped — never zero/negative or it'd starve the UI.
    assert r2._refresh_hz >= 1.0
    r3 = StreamingRenderer(console=console, refresh_per_second=10.0)
    # Legacy flush interval is the inverse of the refresh rate.
    assert abs(r3._legacy_flush_interval - 0.1) < 1e-6


@pytest.mark.asyncio
async def test_legacy_path_renders_markdown_headings():
    """Regression: the previous legacy path printed deltas raw, so
    ``# heading`` arrived as literal ``#``s in the terminal. The fix
    must render Markdown headings even when Live is disabled."""
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=True, markdown=True, refresh_per_second=30.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="# Title\n\n"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="body line\n"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Heading text survives.
    assert "Title" in output
    assert "body line" in output
    # Literal ``#`` heading marker must NOT appear in the rendered output.
    assert "# Title" not in output


@pytest.mark.asyncio
async def test_live_path_refresh_is_driven_from_async_loop():
    """Regression: with auto_refresh=True the Rich background thread can be
    starved by the asyncio loop, leaving the screen blank until the stream
    ends. The renderer must drive Live.refresh() from the async loop so
    every event produces output deterministically.

    We assert via the public Live API that the renderer disables Rich's
    auto-refresh thread (so refresh is OUR responsibility, not theirs).
    """
    import inspect

    from deile.ui import streaming_renderer as sr_module

    src = inspect.getsource(sr_module.StreamingRenderer._render_live)
    assert "auto_refresh=False" in src, (
        "Live must be opened with auto_refresh=False so the async loop "
        "controls redraws — otherwise output is buffered until stream end."
    )
    assert "live.refresh()" in src, (
        "Renderer must call live.refresh() explicitly from the async loop."
    )


@pytest.mark.asyncio
async def test_live_path_renders_progressively_without_thread_starvation():
    """End-to-end check on the Live path: every text delta + tool block
    must reach the captured console by the time the stream ends, even
    when events arrive back-to-back without yielding to a background
    thread (simulating the thread-starvation scenario in production).
    """
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=False, markdown=True, refresh_per_second=30.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Você pediu: "),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="conte os arquivos."),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="list_files",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="list_files",
            arguments={"path": "."},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="list_files",
            tool_status="success",
            tool_result_summary="42 files",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=10, output_tokens=4),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Text and tool block both reached the console by stream end.
    assert "Você pediu:" in output
    assert "conte os arquivos." in output
    assert "list_files" in output
    assert "42 files" in output
    # The first-line prefix must appear AT MOST a small number of times —
    # never repeated per-character (regression guard for the bug where each
    # delta append-printed the full accumulated text).
    assert output.count("Você pediu:") <= 3, (
        f"first-line prefix repeated {output.count('Você pediu:')} times — "
        "cursor repositioning is broken (wall-of-text regression)"
    )
    assert result.tool_invocations == 1


@pytest.mark.asyncio
async def test_completed_blocks_commit_to_scrollback_in_order():
    """Regression: in long streams the Live region grew taller than the
    terminal and Rich's cursor positioning failed, causing earlier text
    to "jump" above tool blocks. The renderer must commit completed
    blocks (text whose stream has moved on, or finished tools) into
    scrollback as ``console.print`` calls, leaving Live with only the
    one block currently being modified.

    We assert: the captured console contains TextBlock1 BEFORE the first
    tool's args appear in the output. If the renderer batched everything
    into a single Live group, neither would have been ``console.print``ed
    and the relative order would be a property of the final group, not
    the time-ordered emit stream.
    """
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=False, markdown=False, refresh_per_second=60.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="UNIQUE_PREAMBLE_42"),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="alpha_tool",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="alpha_tool",
            arguments={"k": "v1"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="alpha_tool",
            tool_status="success",
            tool_result_summary="ok",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t2",
            tool_name="beta_tool",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t2",
            tool_name="beta_tool",
            arguments={"k": "v2"},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t2",
            tool_name="beta_tool",
            tool_status="success",
            tool_result_summary="ok2",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()

    pre_idx = output.find("UNIQUE_PREAMBLE_42")
    alpha_idx = output.find("alpha_tool")
    beta_idx = output.find("beta_tool")
    assert pre_idx >= 0, "preamble text was never rendered"
    assert alpha_idx >= 0, "alpha tool was never rendered"
    assert beta_idx >= 0, "beta tool was never rendered"
    # Order in the captured stream MUST match emit order — preamble first,
    # then alpha, then beta. If Live had buffered them into one Group at
    # exit time these ordering checks would be subject to render layout
    # but not to commit-to-scrollback timing; the assertion still holds
    # for the correct implementation.
    assert pre_idx < alpha_idx < beta_idx, (
        f"out-of-order render: preamble@{pre_idx} alpha@{alpha_idx} beta@{beta_idx}"
    )


@pytest.mark.asyncio
async def test_committed_blocks_keep_blank_line_separation():
    """Regression: when blocks are flushed to scrollback (per-block Live
    refactor), each committed block must be followed by a blank line so
    consecutive tool blocks aren't glued together. Without this, the
    user sees:

        ✓ tool_a(...)
          summary_a
        ✓ tool_b(...)
          summary_b

    instead of the expected (with a blank line between them):

        ✓ tool_a(...)
          summary_a

        ✓ tool_b(...)
          summary_b
    """
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=False, markdown=False, refresh_per_second=60.0
    )
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t1",
            tool_name="tool_a",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1",
            tool_name="tool_a",
            arguments={"x": 1},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1",
            tool_name="tool_a",
            tool_status="success",
            tool_result_summary="summary_a",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_START,
            tool_call_id="t2",
            tool_name="tool_b",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t2",
            tool_name="tool_b",
            arguments={"y": 2},
        ),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t2",
            tool_name="tool_b",
            tool_status="success",
            tool_result_summary="summary_b",
        ),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    await renderer.render(_replay(events))
    output = console.file.getvalue()

    # Locate the two tool names in the output and verify there's at
    # least one blank line between them (commit step adds it).
    a_idx = output.find("tool_a")
    b_idx = output.find("tool_b")
    assert a_idx >= 0 and b_idx >= 0
    between = output[a_idx:b_idx]
    # A blank line shows up as two consecutive newlines in the captured
    # text stream. We require at least one such pair between the tools.
    assert "\n\n" in between, (
        "no blank line separating consecutive committed tool blocks — "
        "tool blocks would be visually glued together"
    )


@pytest.mark.asyncio
async def test_blocks_have_visual_separation_in_compose():
    """Blocks rendered back-to-back must have a blank line between them so
    tool blocks and text blocks aren't 'glued together' on screen.

    Regression: the previous _compose Group joined items without
    spacers, so the user saw 'message1<no newline>● tool(args)<no
    newline>● tool2(args)' all on adjacent lines.
    """
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=False)

    # Build composition manually (don't go through render — we want to
    # introspect the Group children, not measure stdout).
    from rich.console import Group
    from rich.text import Text

    from deile.ui.streaming_renderer import _TextBlock, _ToolBlock

    blocks = [
        _TextBlock(text="hello"),
        _ToolBlock(tool_call_id="t1", tool_name="bash_execute", args={"cmd": "ls"}, status="running"),
        _ToolBlock(tool_call_id="t2", tool_name="read_file", args={"path": "x"}, status="success"),
        _TextBlock(text="done"),
    ]
    group = renderer._compose(blocks)
    assert isinstance(group, Group)
    items = list(group.renderables)
    # 4 content items + 3 spacers between them = 7 total
    assert len(items) == 7, f"expected 7 items (4 content + 3 spacers), got {len(items)}"
    # Spacers are empty Text — they sit at odd indices (1, 3, 5).
    for i in (1, 3, 5):
        assert isinstance(items[i], Text) and str(items[i]) == "", (
            f"item at index {i} should be a blank-line spacer, got {items[i]!r}"
        )


@pytest.mark.asyncio
async def test_markdown_disabled_falls_back_to_plain_text_legacy():
    """When markdown=False, the legacy path emits plain text (markers
    survive verbatim) — useful for capturing tests that assert raw text."""
    console = _capture_console()
    renderer = StreamingRenderer(
        console=console, legacy_windows=True, markdown=False, refresh_per_second=30.0
    )
    events = [
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="**bold** done"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "**bold**" in output  # Markers preserved when markdown=False.
    assert result.full_text == "**bold** done"


@pytest.mark.asyncio
async def test_error_block_does_not_show_rich_markup_as_literal_text_legacy():
    """Regression: error blocks must be rendered via Text.from_markup, not
    Markdown/Text, so [red]...[/red] tags appear as colour, not literal chars."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=True)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.ERROR,
            error_envelope={"message": "budget exceeded", "error_type": "BudgetExceeded"},
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert result.error_message == "budget exceeded"
    assert "[red]" not in output
    assert "budget exceeded" in output


@pytest.mark.asyncio
async def test_error_block_does_not_show_rich_markup_as_literal_text_live():
    """Same regression, live (non-legacy) path."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=True)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.ERROR,
            error_envelope={"message": "some error", "error_type": "ProviderError"},
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert result.error_message == "some error"
    assert "[red]" not in output
    assert "some error" in output


@pytest.mark.asyncio
async def test_stage_events_update_spinner_label_legacy():
    """STAGE events emitted before the first content event must surface as
    progress lines on the legacy path (no Live region available)."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Parsing input"),
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Selecting provider"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="hello"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "Parsing input" in output
    assert "Selecting provider" in output
    assert result.full_text == "hello"


@pytest.mark.asyncio
async def test_stage_events_do_not_pollute_text_buffer_live():
    """STAGE events must not be appended to the text accumulator — they're
    advisory progress signals, not assistant content."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=False, markdown=True)
    events = [
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Building context"),
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Connecting"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="Olá!"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=1, output_tokens=1),
        ),
    ]
    result = await renderer.render(_replay(events))
    assert result.full_text == "Olá!"  # No stage labels leaked into content


@pytest.mark.asyncio
async def test_stage_indicator_visible_mid_turn_between_tool_and_next_text():
    """The renderer must surface a STAGE between a tool result and the next
    LLM response — that's the gap where the UI used to go silent."""
    console = _capture_console()
    # Legacy path so the captured StringIO contains every printed line —
    # the live path uses Rich.Live which doesn't reliably flush transient
    # frames to a non-TTY capture file.
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=True)
    events = [
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Awaiting first token"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="vou listar arquivos"),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_USE_END,
            tool_call_id="t1", tool_name="list_files", arguments={},
        ),
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Executing list_files"),
        UnifiedStreamEvent(
            type=StreamEventType.TOOL_RESULT,
            tool_call_id="t1", tool_name="list_files",
            tool_status="success", tool_result_summary="3 files",
        ),
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Awaiting next response"),
        UnifiedStreamEvent(type=StreamEventType.TEXT_DELTA, text="encontrei 3"),
        UnifiedStreamEvent(
            type=StreamEventType.USAGE_FINAL,
            usage=ModelUsageSnapshot(input_tokens=2, output_tokens=2),
        ),
    ]
    result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    # Mid-turn stages were surfaced
    assert "Executing list_files" in output
    assert "Awaiting next response" in output
    # Stage labels never leaked into the assistant text buffer
    assert "Awaiting" not in result.full_text
    assert "Executing" not in result.full_text
    assert result.full_text == "vou listar arquivosencontrei 3"


@pytest.mark.asyncio
async def test_stage_event_alone_does_not_count_as_first_event():
    """The renderer must NOT treat a STAGE event as the first content event:
    the spinner stays alive (logically) and the result has no error/text."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(type=StreamEventType.STAGE, stage="Analyzing intent"),
        # no further events — simulates a stream that died after first stage
    ]
    result = await renderer.render(_replay(events))
    assert result.full_text == ""
    assert result.error_message is None


@pytest.mark.asyncio
async def test_budget_exceeded_error_includes_action_hint():
    """budget_exceeded errors must append a user-actionable hint."""
    console = _capture_console()
    renderer = StreamingRenderer(console=console, legacy_windows=True, markdown=False)
    events = [
        UnifiedStreamEvent(
            type=StreamEventType.ERROR,
            error_envelope={
                "message": "Session x would exceed limit",
                "error_type": "BudgetExceeded",
                "budget_exceeded": True,
            },
        ),
    ]
    _result = await renderer.render(_replay(events))
    output = console.file.getvalue()
    assert "Session x would exceed limit" in output
    assert "/model budget" in output
