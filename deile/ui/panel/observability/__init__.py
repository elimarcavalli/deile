"""Live observability surfaces for the DEILE cluster (issue #347).

Three TUI screens consume structured state published by:

* ``claude-worker`` Pod — new ``/v1/sessions*`` endpoints documented in
  ``infra/k8s/claude_worker_server.py``.
* ``deile-pipeline`` Pod — new ``/v1/pipeline-status*`` endpoints documented
  in ``infra/k8s/pipeline_status_server.py``.

Public entrypoints:

* :class:`ClaudeJsonlParser` — incremental parser for
  ``~/.claude/projects/<workspace-hash>/<session-uuid>.jsonl`` produced by
  the Claude CLI.
* :class:`ClusterObservabilityClient` — thin HTTP client that fan-outs to
  the pipeline status server and the claude-worker session endpoints.
* :class:`ClusterStatusScreen`, :class:`LiveSessionScreen`,
  :class:`HistoryScreen` — Rich-based renderers consumed by the panel main
  loop.
"""

from deile.ui.panel.observability.jsonl_parser import (  # noqa: F401
    AssistantTurn,
    ClaudeJsonlParser,
    ToolResultTurn,
    ToolUseTurn,
    Turn,
    UnknownTurn,
    UserTurn,
)
