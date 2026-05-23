---
name: tdd
description: Test-Driven Development cycle and fixture/mocking patterns
triggers:
  file_globs: ["test_*.py", "*_test.py", "*.test.ts", "*.spec.ts", "*.spec.js"]
  code_block_langs: []
priority: 40
---
# TDD discipline

When writing or modifying tests, follow these rules:

## The cycle
1. **Red** — write the smallest failing test that captures one behavioral expectation.
2. **Green** — write the minimum production code to pass the test. Resist the urge to refactor here.
3. **Refactor** — only with green tests, clean up duplication and naming. If the refactor goes red, you over-stepped.

## Naming
- Test name should read as a sentence: `test_persona_switch_emits_event_when_id_differs`. Avoid `test_persona_1`, `test_basic`, `test_works`.
- Group tests in a class only when they share fixtures meaningfully; a class with one test is just noise.

## Arrange / Act / Assert
- Visually separate the three blocks with a blank line. If a test has more than one Act, split it.
- Prefer a single behavioral assertion per test; use multiple `assert` statements only when they describe the same behavior from different angles.

## Fixtures
- Fixtures are setup, not assertion. If your fixture calls `assert`, hoist that into the test itself.
- Scope fixtures narrowly (`function` default). Promote to `module`/`session` only with measured proof it's safe and faster.
- For temp files use `tmp_path` (Path) over `tmpdir` (legacy py.path). For env vars use `monkeypatch.setenv`.

## Mocking
- Mock at the boundary you own, not third-party internals. Patch `mymodule.requests` (your import) not `requests.get` (global).
- A test that mocks the function under test is a tautology — delete it.
- Prefer fakes (small in-memory implementations of the real interface) over `Mock(spec=...)` chains; fakes survive refactors better.

## Coverage
- Coverage is a smoke detector, not a goal. 100% on trivial getters with 30% on the routing logic is a worse signal than 70% balanced.
- Branch coverage catches more real bugs than line coverage; turn it on if your runner supports it.
