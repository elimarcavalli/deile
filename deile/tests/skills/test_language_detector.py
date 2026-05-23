"""Tests for ``deile.skills.language_detector``."""

from __future__ import annotations

import pytest

from deile.skills.language_detector import LanguageDetector, default_extension_map


@pytest.mark.unit
class TestExtensionMap:
    def test_known_extensions_resolve(self) -> None:
        det = LanguageDetector()
        assert det.language_for_path("src/foo.py") == "python"
        assert det.language_for_path("App.tsx") == "typescript"
        assert det.language_for_path("/abs/path/lib.rs") == "rust"

    def test_unknown_extension_returns_none(self) -> None:
        det = LanguageDetector()
        assert det.language_for_path("data.xyz") is None
        assert det.language_for_path("") is None

    def test_extension_lookup_is_case_insensitive(self) -> None:
        det = LanguageDetector()
        assert det.language_for_path("Foo.PY") == "python"
        assert det.language_for_path("X.TsX") == "typescript"

    def test_basename_overrides_apply(self) -> None:
        det = LanguageDetector()
        assert det.language_for_path("Dockerfile") == "dockerfile"
        assert det.language_for_path("path/to/Makefile") == "make"

    def test_extension_map_override_via_constructor(self) -> None:
        det = LanguageDetector(extension_map={".zig": "zig", ".py": "starlark"})
        assert det.language_for_path("foo.zig") == "zig"
        # User override wins over the built-in default.
        assert det.language_for_path("foo.py") == "starlark"

    def test_extension_override_accepts_keys_without_dot(self) -> None:
        det = LanguageDetector(extension_map={"nim": "nim"})
        assert det.language_for_path("a.nim") == "nim"

    def test_languages_for_paths_is_unique_and_ordered(self) -> None:
        det = LanguageDetector()
        out = det.languages_for_paths(["a.py", "b.py", "c.ts", "a.py", "d.rs"])
        assert out == ["python", "typescript", "rust"]

    def test_default_extension_map_helper_returns_copy(self) -> None:
        a = default_extension_map()
        a[".py"] = "mutated"
        assert default_extension_map()[".py"] == "python"


@pytest.mark.unit
class TestCodeBlockExtraction:
    def test_extracts_single_fence_language(self) -> None:
        det = LanguageDetector()
        text = "Look:\n```python\nx = 1\n```"
        assert det.langs_in_code_blocks(text) == ["python"]

    def test_extracts_multiple_languages_unique_and_ordered(self) -> None:
        det = LanguageDetector()
        text = "```python\na\n```\nthen\n```ts\nb\n```\nthen\n```python\nc\n```"
        assert det.langs_in_code_blocks(text) == ["python", "ts"]

    def test_fence_without_language_is_ignored(self) -> None:
        det = LanguageDetector()
        text = "```\nplain code\n```"
        assert det.langs_in_code_blocks(text) == []

    def test_empty_input_returns_empty_list(self) -> None:
        det = LanguageDetector()
        assert det.langs_in_code_blocks("") == []

    def test_language_tag_is_normalized_to_lowercase(self) -> None:
        det = LanguageDetector()
        text = "```Python\nx\n```"
        assert det.langs_in_code_blocks(text) == ["python"]
