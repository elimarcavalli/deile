"""Runner that executes every standalone test script in this folder.

This folder holds two kinds of files, distinguished by name:
  * **Pytest tests** — files starting with ``test_``. Run via ``pytest``.
  * **Standalone scripts** — anything else (commonly ``*_test.py`` or
    ``smoke_test_*.py``). These are what this runner executes.

The naming split mirrors ``pytest.ini`` (``python_files = test_*.py *_test.py``):
both patterns are pytest-collectable, but pytest only finds ``Test*`` classes /
``test_*`` functions, of which standalone scripts have none — so they coexist
safely. We pick the standalone set by *excluding* the ``test_*`` prefix.

A defensive second check rejects any script lacking an ``if __name__ ==
"__main__":`` execution block, so importing-only files are not run.

Each script runs in a fresh subprocess so they can't leak global state into
each other (the bigger integration tests cache singletons in process memory).

Exit code: 0 if every script exited 0, 1 otherwise.

Usage:
    python deile/tests/all.py
    python deile/tests/all.py --verbose      # stream output as it runs
    python deile/tests/all.py --filter smoke # only run scripts matching substring
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import time
from pathlib import Path

THIS_FILE = Path(__file__).resolve()
TESTS_DIR = THIS_FILE.parent


def _has_main_guard(path: Path) -> bool:
    """True iff the file is meant to be executed (has an ``if __name__ == ...``).

    Done with the ``ast`` module, not substring search — earlier revisions used
    ``"__name__ == \"__main__\""`` text matching and got fooled by string
    literals inside fixtures.
    """
    import ast

    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (OSError, SyntaxError):
        return False
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        cond = node.test
        if (
            isinstance(cond, ast.Compare)
            and isinstance(cond.left, ast.Name)
            and cond.left.id == "__name__"
            and len(cond.comparators) == 1
            and isinstance(cond.comparators[0], ast.Constant)
            and cond.comparators[0].value == "__main__"
        ):
            return True
    return False


def discover_tests(pattern: str | None) -> list[Path]:
    candidates = sorted(p for p in TESTS_DIR.glob("*.py") if p.is_file())
    out: list[Path] = []
    for p in candidates:
        if p == THIS_FILE:
            continue
        if p.name.startswith("_"):
            continue
        # Pytest tests use the ``test_*`` prefix (project convention, see
        # pytest.ini). Anything starting with that runs through pytest, not
        # through this script.
        if p.name.startswith("test_"):
            continue
        if not _has_main_guard(p):
            continue
        if pattern and pattern not in p.name:
            continue
        out.append(p)
    return out


PROJECT_ROOT = TESTS_DIR.parent.parent  # repo root: scripts live two levels up


def run_one(script: Path, *, verbose: bool) -> tuple[bool, float, str]:
    start = time.perf_counter()
    if verbose:
        # Stream live output to the terminal — useful when an individual
        # script hangs, so the user sees where it stopped.
        proc = subprocess.run(
            [sys.executable, str(script)],
            cwd=PROJECT_ROOT,
        )
        elapsed = time.perf_counter() - start
        return proc.returncode == 0, elapsed, ""

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )
    elapsed = time.perf_counter() - start
    tail = ""
    if proc.returncode != 0:
        # Show only the tail to keep summary readable — full output is
        # one rerun away with --verbose.
        stderr_tail = proc.stderr.strip().splitlines()[-15:]
        stdout_tail = proc.stdout.strip().splitlines()[-15:]
        tail = "\n".join(stdout_tail + stderr_tail)
    return proc.returncode == 0, elapsed, tail


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="stream child output live"
    )
    parser.add_argument(
        "--filter",
        "-f",
        default=None,
        help="only run scripts whose filename contains this substring",
    )
    args = parser.parse_args()

    scripts = discover_tests(args.filter)
    if not scripts:
        print("no test scripts found", file=sys.stderr)
        return 1

    print(f"Discovered {len(scripts)} script(s) in {TESTS_DIR}")
    for s in scripts:
        print(f"  - {s.name}")
    print()

    results: list[tuple[Path, bool, float, str]] = []
    for script in scripts:
        print(f"[run] {script.name} ...", flush=True)
        ok, elapsed, tail = run_one(script, verbose=args.verbose)
        marker = "PASS" if ok else "FAIL"
        print(f"  -> {marker} ({elapsed:.1f}s)")
        if not ok and tail and not args.verbose:
            for line in tail.splitlines():
                print(f"      | {line}")
        results.append((script, ok, elapsed, tail))

    total = len(results)
    passed = sum(1 for _, ok, _, _ in results if ok)
    failed = total - passed
    total_time = sum(t for _, _, t, _ in results)

    print()
    print("=" * 70)
    print(f"Summary: {passed}/{total} passed, {failed} failed, {total_time:.1f}s total")
    if failed:
        print("Failures:")
        for script, ok, elapsed, _ in results:
            if not ok:
                print(f"  - {script.name} ({elapsed:.1f}s)")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
