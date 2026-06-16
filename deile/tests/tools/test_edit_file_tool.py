"""Test suite for EditFileTool.

EditFileTool ships three load-bearing guarantees that the test suite must
defend:

1. **Atomicidade**: either ALL patches in a call land on disk, or NONE do.
   No partial application, no half-written buffer published.
2. **Determinismo**: patches apply in order; patch N sees the buffer produced
   by patches 1..N-1.
3. **Não-ambiguidade**: unless ``replace_all=true``, ``find`` MUST match exactly
   once. 0 matches and >=2 matches are both errors with diagnostic messages.

The atomic-write integrity is shared with ``WriteFileTool`` and covered by
``test_write_file_atomic.py``; here we only confirm that ``edit_file`` plumbs
through the same helper and that failures preserve the original file.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from deile.tools.base import ToolContext, ToolStatus
from deile.tools.file_tools import EditFileTool, WriteFileTool


@pytest.fixture
def tool() -> EditFileTool:
    return EditFileTool()


def _ctx(workdir: Path, **args: Any) -> ToolContext:
    return ToolContext(
        user_input="",
        parsed_args=args,
        working_directory=str(workdir),
    )


# ---------------------------------------------------------------------------
# Happy path — single and multi-patch edits
# ---------------------------------------------------------------------------


class TestSinglePatch:
    def test_unique_find_is_replaced(self, tool: EditFileTool, tmp_path: Path) -> None:
        target = tmp_path / "hello.py"
        target.write_text("greeting = 'hello'\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="hello.py",
                patches=[{"find": "'hello'", "replace": "'world'"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "greeting = 'world'\n"

    def test_multiline_replacement(self, tool: EditFileTool, tmp_path: Path) -> None:
        target = tmp_path / "multi.txt"
        target.write_text("line1\nline2\nline3\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="multi.txt",
                patches=[{"find": "line1\nline2\n", "replace": "alpha\nbeta\ngamma\n"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "alpha\nbeta\ngamma\nline3\n"

    def test_replace_with_empty_string_deletes_match(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "x.txt"
        target.write_text("KEEP\nDELETE_THIS\nKEEP\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="x.txt",
                patches=[{"find": "DELETE_THIS\n", "replace": ""}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "KEEP\nKEEP\n"

    def test_old_string_new_string_compatibility_shape(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        """Ergonomic single-edit shape (old_string/new_string) routes through
        the same engine as patches=[{find, replace}]."""
        target = tmp_path / "compat.py"
        target.write_text("x = 1\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="compat.py",
                old_string="x = 1",
                new_string="x = 42",
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "x = 42\n"


class TestMultiPatch:
    def test_multiple_patches_apply_in_order(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ordered.txt"
        target.write_text("A\nB\nC\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="ordered.txt",
                patches=[
                    {"find": "A\n", "replace": "alpha\n"},
                    {"find": "B\n", "replace": "bravo\n"},
                    {"find": "C\n", "replace": "charlie\n"},
                ],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "alpha\nbravo\ncharlie\n"

    def test_patch_can_depend_on_previous_patch_result(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        """Patch 2 finds a string introduced by patch 1 — proves sequential
        application against the *current* buffer, not against the original."""
        target = tmp_path / "chain.txt"
        target.write_text("foo\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="chain.txt",
                patches=[
                    {"find": "foo", "replace": "INTERMEDIATE"},
                    {"find": "INTERMEDIATE", "replace": "final"},
                ],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "final\n"


class TestReplaceAll:
    def test_replace_all_handles_repeated_match(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "rep.txt"
        target.write_text("foo bar foo baz foo\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="rep.txt",
                patches=[{"find": "foo", "replace": "FOO", "replace_all": True}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "FOO bar FOO baz FOO\n"
        # The applied_log records both occurrences.
        applied = result.metadata["patches_applied"]
        assert applied[0]["occurrences"] == 3
        assert applied[0]["replaced"] == 3

    def test_replace_all_with_single_match_still_works(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        """``replace_all=true`` accepts ≥1 match — does not require ≥2."""
        target = tmp_path / "rep1.txt"
        target.write_text("only one foo here\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="rep1.txt",
                patches=[{"find": "foo", "replace": "FOO", "replace_all": True}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "only one FOO here\n"


# ---------------------------------------------------------------------------
# Error paths — atomicidade & diagnóstico
# ---------------------------------------------------------------------------


class TestFindNotFound:
    def test_zero_occurrences_aborts_with_original_intact(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ola.py"
        original = "print('original')\n"
        target.write_text(original, encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="ola.py",
                patches=[{"find": "WILL_NEVER_MATCH", "replace": "x"}],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "not present" in result.message
        # File must be byte-identical to before.
        assert target.read_text(encoding="utf-8") == original

    def test_near_match_hint_when_whitespace_differs(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ws.py"
        target.write_text("def hello_world():\n    return 1\n", encoding="utf-8")

        # Same logical line but with extra leading spaces — won't match.
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="ws.py",
                patches=[
                    {"find": "    def hello_world():", "replace": "    def goodbye():"}
                ],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "whitespace" in result.message or "DOES appear" in result.message


class TestAmbiguity:
    def test_two_occurrences_without_replace_all_aborts(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "amb.txt"
        original = "foo\nfoo\n"
        target.write_text(original, encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="amb.txt",
                patches=[{"find": "foo", "replace": "bar"}],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "ambiguous" in result.message
        assert "2 occurrences" in result.message
        # Original survives.
        assert target.read_text(encoding="utf-8") == original

    def test_ambiguity_resolved_by_adding_context(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        """The proper LLM response to ambiguity is to widen `find`."""
        target = tmp_path / "amb2.txt"
        target.write_text("greet = foo\nfarewell = foo\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="amb2.txt",
                patches=[{"find": "greet = foo", "replace": "greet = bar"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "greet = bar\nfarewell = foo\n"


class TestAtomicityAcrossPatches:
    def test_second_patch_fails_first_patch_is_rolled_back(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "tx.txt"
        original = "FIRST\nSECOND\n"
        target.write_text(original, encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="tx.txt",
                patches=[
                    {"find": "FIRST", "replace": "first_done"},
                    {"find": "DOES_NOT_EXIST", "replace": "boom"},
                ],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert result.metadata["failed_patch_index"] == 2
        # The first patch's effect must NOT survive: original on disk.
        assert target.read_text(encoding="utf-8") == original

    def test_third_patch_ambiguous_aborts_whole_transaction(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "tx3.txt"
        original = "A\nB\nC\nD\nC\n"  # 'C' appears twice
        target.write_text(original, encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="tx3.txt",
                patches=[
                    {"find": "A", "replace": "a"},
                    {"find": "B", "replace": "b"},
                    # Ambiguous: 'C' appears twice in the original AND remains
                    # twice after patches 1+2.
                    {"find": "C", "replace": "c"},
                ],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert result.metadata["failed_patch_index"] == 3
        assert target.read_text(encoding="utf-8") == original


class TestInputValidation:
    def test_missing_file_path_errors(self, tool: EditFileTool, tmp_path: Path) -> None:
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                patches=[{"find": "x", "replace": "y"}],
            )
        )
        assert result.status == ToolStatus.ERROR
        assert "file_path" in result.message.lower()

    def test_missing_patches_errors(self, tool: EditFileTool, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("anything\n", encoding="utf-8")
        result = tool.execute_sync(_ctx(tmp_path, file_path="x.txt"))
        assert result.status == ToolStatus.ERROR
        assert "patches" in result.message.lower()

    def test_empty_patches_array_errors(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        (tmp_path / "x.txt").write_text("anything\n", encoding="utf-8")
        result = tool.execute_sync(_ctx(tmp_path, file_path="x.txt", patches=[]))
        assert result.status == ToolStatus.ERROR
        assert (
            "non-empty" in result.message.lower() or "patches" in result.message.lower()
        )

    def test_patch_not_a_dict_errors(self, tool: EditFileTool, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("anything\n", encoding="utf-8")
        result = tool.execute_sync(
            _ctx(tmp_path, file_path="x.txt", patches=["not-a-dict"])
        )
        assert result.status == ToolStatus.ERROR
        assert "patch #1" in result.message

    def test_empty_find_string_rejected(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        (tmp_path / "x.txt").write_text("anything\n", encoding="utf-8")
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="x.txt",
                patches=[{"find": "", "replace": "x"}],
            )
        )
        assert result.status == ToolStatus.ERROR
        assert "empty" in result.message.lower()

    def test_non_string_find_rejected(self, tool: EditFileTool, tmp_path: Path) -> None:
        (tmp_path / "x.txt").write_text("anything\n", encoding="utf-8")
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="x.txt",
                patches=[{"find": 42, "replace": "x"}],
            )
        )
        assert result.status == ToolStatus.ERROR
        assert "find" in result.message.lower()

    def test_nonexistent_file_errors_clearly(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="does_not_exist.txt",
                patches=[{"find": "x", "replace": "y"}],
            )
        )
        assert result.status == ToolStatus.ERROR
        assert "not found" in result.message.lower()
        assert "write_file" in result.message  # suggests the right alternative

    def test_directory_path_errors(self, tool: EditFileTool, tmp_path: Path) -> None:
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="subdir",
                patches=[{"find": "x", "replace": "y"}],
            )
        )
        assert result.status == ToolStatus.ERROR
        assert "not a file" in result.message.lower()


class TestEncoding:
    def test_non_utf8_file_rejected_with_clear_message(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "latin.txt"
        # Pure non-UTF-8 byte sequence (0xff is invalid as standalone UTF-8).
        target.write_bytes(b"\xff\xfe\xfd")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="latin.txt",
                patches=[{"find": "x", "replace": "y"}],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "UTF-8" in result.message

    def test_utf8_emoji_payload_preserved_byte_for_byte(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "emoji.txt"
        original = "Olá 🌍\nGoodbye 👋\n"
        target.write_text(original, encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="emoji.txt",
                patches=[{"find": "Goodbye 👋", "replace": "Hello 🙂"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert (tmp_path / "emoji.txt").read_bytes() == "Olá 🌍\nHello 🙂\n".encode(
            "utf-8"
        )


class TestNoOp:
    def test_find_equals_replace_does_not_rewrite_file(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "noop.txt"
        original = "stay the same\n"
        target.write_text(original, encoding="utf-8")
        original_mtime = target.stat().st_mtime_ns

        # Wait a hair so mtime differs if the file is rewritten.
        import time

        time.sleep(0.01)

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="noop.txt",
                patches=[{"find": "stay the same", "replace": "stay the same"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert result.metadata.get("no_op") is True
        # File content is unchanged.
        assert target.read_text(encoding="utf-8") == original
        # And we did not rewrite — mtime is the same.
        assert target.stat().st_mtime_ns == original_mtime


class TestValidationHint:
    def test_python_file_emits_post_write_validation_hint(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ok.py"
        target.write_text("x = 1\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="ok.py",
                patches=[{"find": "x = 1", "replace": "x = 42"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert result.metadata.get("post_write_validation_required") is True
        cmd = result.metadata.get("post_write_validation_command", "")
        assert "py_compile" in cmd
        assert "ok.py" in cmd

    def test_text_file_does_not_emit_validation_hint(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "notes.txt"
        target.write_text("old\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="notes.txt",
                patches=[{"find": "old", "replace": "new"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert "post_write_validation_required" not in result.metadata


class TestAtomicWriteFailurePreservesOriginal:
    def test_failure_in_atomic_publish_leaves_original_untouched(
        self,
        tool: EditFileTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "atomic.txt"
        original = "ORIGINAL CONTENT\n"
        target.write_text(original, encoding="utf-8")

        # Force the underlying atomic publish (used by both write_file and
        # edit_file) to fail. The error must surface as ToolStatus.ERROR and
        # the original file must survive byte-for-byte.
        def boom(_target: Path, _content: str) -> None:
            raise OSError("simulated atomic-write failure")

        monkeypatch.setattr(WriteFileTool, "_atomic_write_text", staticmethod(boom))

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="atomic.txt",
                patches=[{"find": "ORIGINAL", "replace": "MUTATED"}],
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "atomic" in result.message.lower() or "intact" in result.message.lower()
        assert target.read_text(encoding="utf-8") == original


class TestPathNormalization:
    def test_leading_slash_is_normalized_project_relative(
        self, tool: EditFileTool, tmp_path: Path
    ) -> None:
        """LLM might send `/foo.txt` meaning project-relative `foo.txt` —
        edit_file uses the same resolver as write_file/read_file and reports
        the normalization in the result message."""
        (tmp_path / "foo.txt").write_text("abc\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="/foo.txt",
                patches=[{"find": "abc", "replace": "xyz"}],
            )
        )

        assert result.status == ToolStatus.SUCCESS
        assert (
            "PATH_NORMALIZED" in result.message
            or result.metadata.get("path_normalization_note") is not None
        )
        assert (tmp_path / "foo.txt").read_text(encoding="utf-8") == "xyz\n"


class TestToolMetadata:
    def test_name_description_category(self, tool: EditFileTool) -> None:
        assert tool.name == "edit_file"
        assert tool.category == "file"
        assert (
            "edit_file" in tool.description.lower()
            or "edit" in tool.description.lower()
        )
        assert "write_file" in tool.description  # mentions sibling tool to guide LLM


class TestSchema:
    """The JSON schema must declare `patches` as an array of objects so that
    Anthropic/OpenAI/Gemini function-calling APIs accept the tool definition
    and the LLM produces well-formed arguments."""

    def test_schema_file_loads_and_declares_patches_array(self) -> None:
        from deile.tools.base import ToolSchema

        schema_path = (
            Path(__file__).resolve().parents[2] / "tools" / "schemas" / "edit_file.json"
        )
        assert schema_path.exists(), f"schema missing at {schema_path}"
        schema = ToolSchema.from_json_file(schema_path)
        params = schema.parameters
        props = params["properties"]
        assert props["file_path"]["type"] == "STRING"
        assert props["patches"]["type"] == "ARRAY"
        item_props = props["patches"]["items"]["properties"]
        assert item_props["find"]["type"] == "STRING"
        assert item_props["replace"]["type"] == "STRING"
        assert item_props["replace_all"]["type"] == "BOOLEAN"
        assert set(params["required"]) == {"file_path", "patches"}

    def test_schema_converts_to_anthropic_format(self) -> None:
        from deile.tools.base import ToolSchema

        schema_path = (
            Path(__file__).resolve().parents[2] / "tools" / "schemas" / "edit_file.json"
        )
        schema = ToolSchema.from_json_file(schema_path)
        anthropic_tool = schema.to_anthropic_tool()
        assert anthropic_tool["name"] == "edit_file"
        # The conversion lowercases JSON Schema types.
        assert (
            anthropic_tool["input_schema"]["properties"]["patches"]["type"] == "array"
        )
