"""Direct unit tests for helpers in ``deile.tools._path_resolution``.

These cover the pure helpers that ``file_tools.py`` depends on but that
previously had no targeted tests of their own — ``_extract_path_arg`` and
``_not_found_message``. Most existing coverage came in via the higher-level
``ReadFileTool``/``WriteFileTool`` tests, which made regressions in the
helpers easy to miss when the surrounding tool also changed.
"""

from __future__ import annotations

import pytest

from deile.tools._path_resolution import (_PATH_ARG_KEYS_EDIT,
                                          _PATH_ARG_KEYS_FALLBACK,
                                          _PATH_ARG_KEYS_PRIMARY,
                                          _PATH_ARG_KEYS_WRITE,
                                          LocalFileAccessViolation,
                                          ResolvedPath, _extract_path_arg,
                                          _not_found_message,
                                          _resolve_project_path)

# ---------------------------------------------------------------------------
# _extract_path_arg
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_extract_path_arg_default_precedence_picks_file_path_first():
    """``file_path`` wins over every synonym under the default key tuple."""
    args = {
        "file_path": "from_file_path",
        "path": "from_path",
        "filename": "from_filename",
        "file": "from_file",
        "filepath": "from_filepath",
    }
    assert _extract_path_arg(args) == "from_file_path"


@pytest.mark.unit
def test_extract_path_arg_write_precedence_pins_filename_over_path():
    """``WriteFileTool``'s historical order is ``file_path > filename > path``.

    With ``path`` and ``filename`` both supplied (and ``file_path`` absent),
    the write-specific tuple must prefer ``filename``.
    """
    args = {"filename": "winner", "path": "loser"}
    assert _extract_path_arg(args, keys=_PATH_ARG_KEYS_WRITE) == "winner"


@pytest.mark.unit
def test_extract_path_arg_edit_precedence_pins_path_over_filename():
    """``EditFileTool``'s historical order is ``file_path > path > filename``.

    Mirror of the write test: under the edit tuple, the same inputs must
    resolve in the opposite direction.
    """
    args = {"filename": "loser", "path": "winner"}
    assert _extract_path_arg(args, keys=_PATH_ARG_KEYS_EDIT) == "winner"


@pytest.mark.unit
def test_extract_path_arg_two_stage_lookup_primary_vs_fallback():
    """Read/Delete split the lookup around ``file_list`` — the helper must
    accept arbitrary key tuples so the caller can stage the lookup."""
    args = {"filename": "synonym", "path": "primary"}
    # Primary stage finds ``path``…
    assert _extract_path_arg(args, keys=_PATH_ARG_KEYS_PRIMARY) == "primary"
    # …fallback stage finds ``filename`` only when primary is empty.
    args_only_fallback = {"filename": "synonym"}
    assert _extract_path_arg(args_only_fallback, keys=_PATH_ARG_KEYS_PRIMARY) is None
    assert (
        _extract_path_arg(args_only_fallback, keys=_PATH_ARG_KEYS_FALLBACK)
        == "synonym"
    )


@pytest.mark.unit
def test_extract_path_arg_returns_none_for_empty_dict():
    """No keys present → ``None``, never empty string or KeyError."""
    assert _extract_path_arg({}) is None


@pytest.mark.unit
def test_extract_path_arg_rejects_non_string_values():
    """Lists, dicts and ints used to pass the old truthy check by accident.

    The tightened helper accepts only non-empty ``str`` values; everything
    else (including ``None``) falls through to the next key, then to ``None``.
    """
    args = {
        "file_path": ["not", "a", "string"],
        "path": {"also": "wrong"},
        "filename": 42,
        "file": None,
        "filepath": "actual_path.txt",
    }
    assert _extract_path_arg(args) == "actual_path.txt"


@pytest.mark.unit
def test_extract_path_arg_rejects_empty_string():
    """Empty strings count as missing, not as a valid candidate."""
    args = {"file_path": "", "path": "real_path"}
    assert _extract_path_arg(args) == "real_path"


# ---------------------------------------------------------------------------
# _not_found_message
# ---------------------------------------------------------------------------


def _resolved(
    relative: str = "foo.txt",
    absolute: str = "/work/foo.txt",
    input_: str = "foo.txt",
    note: str | None = None,
) -> ResolvedPath:
    return ResolvedPath(
        absolute=absolute,
        relative_to_cwd=relative,
        input=input_,
        note=note,
    )


@pytest.mark.unit
def test_not_found_message_basic_no_note_no_hint():
    """Minimal case: just relative path + 'File not found:' header."""
    msg = _not_found_message(_resolved(), original_input="foo.txt")
    assert msg == "File not found: foo.txt."


@pytest.mark.unit
def test_not_found_message_includes_normalization_note_when_present():
    """``resolved.note`` flows into the rendered message verbatim."""
    msg = _not_found_message(
        _resolved(note="leading '/' stripped"),
        original_input="/foo.txt",
    )
    assert "input was 'foo.txt'" in msg
    assert "leading '/' stripped" in msg


@pytest.mark.unit
def test_not_found_message_includes_detail_when_provided():
    """The tool-specific middle sentence shows up between header and hint."""
    msg = _not_found_message(
        _resolved(),
        original_input="foo.txt",
        detail="Use list_files to inspect the project tree.",
    )
    assert "Use list_files" in msg


@pytest.mark.unit
def test_not_found_message_omits_bash_hint_when_path_clearly_inside_project():
    """The bash hint only triggers for paths that LOOK outside-project; a
    clean project-relative input without a normalization note must not get
    the bash_execute suggestion."""
    msg = _not_found_message(
        _resolved(),
        original_input="foo.txt",
        include_bash_hint=True,
    )
    assert "bash_execute" not in msg


@pytest.mark.unit
def test_not_found_message_includes_bash_hint_for_outside_project_path():
    """``/etc/...`` is a clear "outside project" intent → bash hint shows up."""
    msg = _not_found_message(
        _resolved(absolute="/etc/passwd"),
        original_input="/etc/passwd",
        include_bash_hint=True,
        bash_verb="cat",
    )
    assert "bash_execute" in msg
    assert "cat " in msg  # the bash verb appears
    assert "/etc/passwd" in msg


@pytest.mark.unit
def test_not_found_message_shell_quotes_path_with_metacharacters():
    """The bash hint must shell-quote ``resolved.absolute`` so paths with
    ``;``, spaces, ``$``, or backticks cannot fabricate a shell command
    inside the message the LLM is about to read back."""
    dangerous_path = "/tmp/foo; rm -rf /"
    msg = _not_found_message(
        _resolved(absolute=dangerous_path),
        original_input="/tmp/foo",  # triggers the bash hint (leading "/")
        include_bash_hint=True,
        bash_verb="cat",
    )
    # The literal "rm -rf /" segment must NOT appear as a bare token: it has
    # to be inside the shell-quoted path, surrounded by single quotes.
    assert "'/tmp/foo; rm -rf /'" in msg
    # And the raw, unquoted dangerous form must not appear as a free-floating
    # part of the suggested command (would mean shlex.quote was skipped).
    assert f'cat {dangerous_path})' not in msg


@pytest.mark.unit
def test_not_found_message_uses_custom_bash_verb():
    """``bash_verb`` defaults to ``cat`` but Delete passes ``rm``."""
    msg = _not_found_message(
        _resolved(absolute="/etc/passwd"),
        original_input="/etc/passwd",
        include_bash_hint=True,
        bash_verb="rm",
    )
    assert "rm " in msg


# ---------------------------------------------------------------------------
# _resolve_project_path — security parity with _not_found_message
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_resolve_project_path_shell_quotes_violation_path(tmp_path):
    """When a path escapes the working directory, the raised
    ``LocalFileAccessViolation`` message suggests ``bash_execute`` with
    ``ls``/``cat`` commands. Just like ``_not_found_message``, the target
    path must be shell-quoted so a dangerous input cannot fabricate a
    shell command inside the message the LLM reads back.
    """
    with pytest.raises(LocalFileAccessViolation) as exc:
        _resolve_project_path("../../etc; rm -rf /", str(tmp_path))
    msg = str(exc.value)
    # The dangerous bare-token sequence must not appear unquoted in the
    # ls/cat suggestion — shlex.quote wraps the whole path in single
    # quotes when it contains shell metacharacters.
    assert "ls ; rm -rf /" not in msg
    assert "cat ; rm -rf /" not in msg
