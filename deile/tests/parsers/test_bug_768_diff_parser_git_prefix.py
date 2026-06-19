"""xfail test for bug #768: DiffParser includes a/ b/ git prefixes in file_references.

Bug: The deduplication guard at diff_parser.py:71 compares the full extracted
path (e.g. 'a/src/foo.py') against the exact strings '/dev/null', 'a/', 'b/'.
These can never match for real file paths, so all git diff headers produce
file_references containing paths like 'a/src/foo.py' instead of 'src/foo.py'.

Fix: Strip leading 'a/' or 'b/' prefix before deduplication.
Tracker: #768
"""

from __future__ import annotations

import pytest

from deile.parsers.diff_parser import DiffParser

UNIFIED_GIT_DIFF = """diff --git a/src/foo.py b/src/foo.py
--- a/src/foo.py\t2024-01-01 00:00:00.000000000 +0000
+++ b/src/foo.py\t2024-01-01 00:00:01.000000000 +0000
@@ -1,3 +1,3 @@
 def hello():
-    return 'old'
+    return 'new'
"""


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 diff-parser-git-prefix — fix pending tracker #768",
)
def test_git_diff_file_references_strip_ab_prefix() -> None:
    """file_references from a standard git diff must not contain 'a/' or 'b/' prefixes.

    When the bug is present:
      - file_references == ['a/src/foo.py', 'b/src/foo.py']
      - assertion below FAILS (bogus is non-empty, not bogus is False) -> xfail -> green

    When fixed:
      - file_references == ['src/foo.py']
      - bogus is empty, assertion PASSES -> xpass -> strict=True marks as FAILED (guard regression)
    """
    parser = DiffParser()
    result = parser.parse(UNIFIED_GIT_DIFF)

    bogus = [r for r in result.file_references if r.startswith("a/") or r.startswith("b/")]

    assert not bogus, (
        f"file_references must not contain a/ or b/ prefixes, got: {bogus}"
    )


@pytest.mark.xfail(
    strict=True,
    reason="bug #768 diff-parser-git-prefix-dedup — fix pending tracker #768",
)
def test_git_diff_single_file_produces_single_reference() -> None:
    """A diff touching one file must produce exactly one file_reference after prefix strip.

    When the bug is present:
      - result is ['a/src/foo.py', 'b/src/foo.py'] (2 items)
      - assertion len == 1 fails -> xfail

    When fixed:
      - result is ['src/foo.py'] (1 item)
      - assertion passes -> xpass
    """
    parser = DiffParser()
    result = parser.parse(UNIFIED_GIT_DIFF)

    assert len(result.file_references) == 1, (
        f"Expected 1 file reference for single-file diff, got {len(result.file_references)}: "
        f"{result.file_references}"
    )
