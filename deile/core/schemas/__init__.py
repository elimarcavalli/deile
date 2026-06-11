"""JSON Schema documents for the worker dispatch contract (issue #620).

The result schema is versioned (``schema_version``) so future format changes
can be migrated retroactively. :data:`RESULT_SCHEMA_VERSION` is the single
source of truth for the current version — both the writer
(``worker_server._run_task``) and any reader import it from here.
"""

from __future__ import annotations

from pathlib import Path

#: Current version of the dispatch result document (issue #620 AC9).
RESULT_SCHEMA_VERSION: int = 1

#: Absolute path to the JSON Schema describing the result document.
RESULT_SCHEMA_PATH: Path = Path(__file__).resolve().parent / "result_v1.json"
