---
name: python
description: Project-specific Python rules for this codebase — async/await usage, CancelledError handling, exception hierarchies, typing conventions, and pytest fixtures. Overrides any generic Python advice you might give from training.
triggers:
  file_globs: ["*.py", "*.pyi", "pyproject.toml", "requirements*.txt"]
  code_block_langs: [python, py]
  keywords: ["CancelledError", "asyncio.gather", "pytest fixture", "async def", "DEILEError"]
priority: 50
---
# Python expertise

When working on Python code, follow these rules:

## Style
- Target Python 3.9+ syntax. Use `dict[str, int]` / `list[str]` style generics; reach for `typing.Optional`/`Union` only when supporting <3.10.
- Prefer f-strings over `%` and `.format()`. Use `!r` for debug-style interpolation of unknown values.
- Reach for `pathlib.Path` over `os.path` when manipulating filesystem paths.

## Typing
- Add type hints on public function signatures. Don't sprinkle `Any` — narrow with `TypeVar`, `Protocol`, or `Literal` where it sharpens intent.
- Use `from __future__ import annotations` in new modules so forward references work without quoting.
- Run `mypy` or `pyright` as a lint, not as a gate — annotate progressively.

## Async
- Never call blocking I/O (`requests`, `time.sleep`, sync DB driver, `open().read()` on a large file) inside `async def`. Wrap blocking calls with `asyncio.to_thread(...)`.
- Use `asyncio.gather(...)` for I/O fan-out; `asyncio.TaskGroup` (3.11+) when you need structured cancellation.
- Always re-raise `asyncio.CancelledError` from any `except` block — swallowing it leaks tasks.
- Use `async with` for resources that need teardown (sessions, DB connections, file locks).

## Errors
- Catch specific exceptions; bare `except:` and `except Exception: pass` are bugs.
- Define domain errors as `class FooError(Exception): ...` rather than reusing `ValueError`/`RuntimeError` for everything.

## Tests (pytest)
- `pytest.ini` markers must be registered before use when `--strict-markers` is on.
- `asyncio_mode = auto` in `pytest.ini` removes the need for `@pytest.mark.asyncio` on every async test.
- Fixtures favoring `tmp_path` over `tmpdir`; `monkeypatch` over manual env var save/restore.
- Parameterize with `pytest.mark.parametrize` instead of for-loops in a single test.

## Common gotchas
- Mutable default arguments (`def f(x=[])`) are evaluated once and shared across calls — use `None` + `x = x or []`.
- `dataclass(frozen=True)` instances are hashable only if all fields are hashable; lists/dicts break this.
- `subprocess.run(..., shell=True)` is a security risk with any interpolated input — use `shell=False` and pass `args` as a list.
- `logging.exception` already includes the traceback; don't pass `exc_info=True` separately.
