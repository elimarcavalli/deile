"""Regression tests for WriteFileTool autonomy + integrity guarantees.

The tool now overwrites by default (no human-in-the-loop confirmation), so it
must compensate with strong write semantics:

* atomic publication via temp file + ``os.replace``
* byte-for-byte read-back validation before publication
* original file preserved on failure
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from deile.tools.base import ToolContext, ToolStatus
from deile.tools.file_tools import WriteFileTool


@pytest.fixture
def tool() -> WriteFileTool:
    return WriteFileTool()


def _ctx(workdir: Path, **args) -> ToolContext:
    return ToolContext(
        user_input="",
        parsed_args=args,
        working_directory=str(workdir),
    )


class TestOverwriteIsDefault:
    def test_overwrites_existing_file_without_explicit_flag(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ola.py"
        target.write_text("print('original')\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(tmp_path, file_path="ola.py", content="print('updated')\n")
        )

        assert result.status == ToolStatus.SUCCESS
        assert target.read_text(encoding="utf-8") == "print('updated')\n"

    def test_explicit_overwrite_false_still_blocks(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        target = tmp_path / "ola.py"
        target.write_text("print('original')\n", encoding="utf-8")

        result = tool.execute_sync(
            _ctx(
                tmp_path,
                file_path="ola.py",
                content="print('updated')\n",
                overwrite=False,
            )
        )

        assert result.status == ToolStatus.ERROR
        assert "already exists" in result.message
        # Original must be untouched.
        assert target.read_text(encoding="utf-8") == "print('original')\n"


class TestAtomicWrite:
    def test_no_leftover_temp_files_on_success(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        result = tool.execute_sync(
            _ctx(tmp_path, file_path="hi.txt", content="x")
        )

        assert result.status == ToolStatus.SUCCESS
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []

    def test_failure_during_publish_preserves_original_and_cleans_temp(
        self,
        tool: WriteFileTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "ola.py"
        target.write_text("print('original')\n", encoding="utf-8")

        original_replace = os.replace

        def boom(src, dst, *args, **kwargs):  # noqa: ANN001
            # Leave the temp where it is; the tool's cleanup must remove it.
            raise OSError("simulated EXDEV during rename")

        monkeypatch.setattr("os.replace", boom)

        result = tool.execute_sync(
            _ctx(tmp_path, file_path="ola.py", content="print('updated')\n")
        )

        # Restore so pytest cleanup works.
        monkeypatch.setattr("os.replace", original_replace)

        assert result.status == ToolStatus.ERROR
        # Original survives — that's the whole point of atomic write.
        assert target.read_text(encoding="utf-8") == "print('original')\n"
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.endswith(".tmp")]
        assert leftovers == []


class TestFidelity:
    def test_bytes_match_exactly_for_unicode_payload(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        # Exotic content: emoji, BOM-like char, mixed newlines, accented text.
        content = "Olá Mundo! 🌍\r\nlinha 2\nlinha 3\n"
        result = tool.execute_sync(
            _ctx(tmp_path, file_path="x.txt", content=content)
        )

        assert result.status == ToolStatus.SUCCESS
        on_disk = (tmp_path / "x.txt").read_bytes()
        assert on_disk == content.encode("utf-8")

    def test_no_newline_translation(
        self, tool: WriteFileTool, tmp_path: Path
    ) -> None:
        # If the tool used text-mode write_text, Windows would translate \n
        # to \r\n. We require raw fidelity on every platform.
        result = tool.execute_sync(
            _ctx(tmp_path, file_path="lf.txt", content="a\nb\nc")
        )

        assert result.status == ToolStatus.SUCCESS
        assert (tmp_path / "lf.txt").read_bytes() == b"a\nb\nc"

    def test_readback_validation_catches_corruption(
        self,
        tool: WriteFileTool,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        target = tmp_path / "ola.py"
        target.write_text("print('original')\n", encoding="utf-8")

        # Simulate a kernel-level corruption between fsync and read-back: the
        # tool re-opens the temp file and gets different bytes. The publish
        # must abort and the original must survive.
        from deile.tools import file_tools

        real_open = open

        def corrupting_open(path, mode="r", *args, **kwargs):  # noqa: ANN001
            fh = real_open(path, mode, *args, **kwargs)
            if mode == "rb" and str(path).endswith(".tmp"):
                class _Fake:
                    def read(self_inner) -> bytes:  # noqa: D401
                        return b"corrupted"

                    def __enter__(self_inner):
                        return self_inner

                    def __exit__(self_inner, *exc):
                        return False

                fh.close()
                return _Fake()
            return fh

        monkeypatch.setattr(file_tools, "open", corrupting_open, raising=False)

        result = tool.execute_sync(
            _ctx(tmp_path, file_path="ola.py", content="print('updated')\n")
        )

        assert result.status == ToolStatus.ERROR
        assert "integrity check failed" in result.message
        # Original must be untouched even though we passed the validation
        # earlier — because we abort BEFORE os.replace.
        assert target.read_text(encoding="utf-8") == "print('original')\n"
