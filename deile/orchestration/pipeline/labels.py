"""Label name constants used as state markers across the autonomous pipeline.

These labels (and their colors / descriptions) are owned by the pipeline. The
``ensure_pipeline_labels`` helper in :mod:`github_client` creates them on the
target repository if they do not already exist.

The ``~`` prefix is a deliberate choice: GitHub sorts labels lexicographically,
and ``~`` is one of the last printable ASCII characters, which keeps pipeline
labels grouped at the end of the label list (out of the way of project labels
like ``bug``, ``enhancement``, ``intent``).
"""

from __future__ import annotations

# Issue workflow ----------------------------------------------------------
WORKFLOW_NEW = "~workflow:nova"
WORKFLOW_REVIEWING = "~workflow:em_revisao"
WORKFLOW_REVIEWED = "~workflow:revisada"
WORKFLOW_IMPLEMENTING = "~workflow:em_implementacao"
WORKFLOW_PR = "~workflow:em_pr"
WORKFLOW_DONE = "~workflow:concluida"

# PR workflow -------------------------------------------------------------
REVIEW_PENDING = "~review:pendente"
REVIEW_IN_PROGRESS = "~review:em_andamento"
REVIEW_CONCLUDED = "~review:concluida"

# Distributed lock --------------------------------------------------------
BATCH_LABEL_PREFIX = "~batch:"

WORKFLOW_LABELS = (
    WORKFLOW_NEW,
    WORKFLOW_REVIEWING,
    WORKFLOW_REVIEWED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_PR,
    WORKFLOW_DONE,
)

REVIEW_LABELS = (REVIEW_PENDING, REVIEW_IN_PROGRESS, REVIEW_CONCLUDED)

LABEL_COLORS = {
    WORKFLOW_NEW: "0e8a16",
    WORKFLOW_REVIEWING: "fbca04",
    WORKFLOW_REVIEWED: "5319e7",
    WORKFLOW_IMPLEMENTING: "1d76db",
    WORKFLOW_PR: "0052cc",
    WORKFLOW_DONE: "0e8a16",
    REVIEW_PENDING: "0e8a16",
    REVIEW_IN_PROGRESS: "fbca04",
    REVIEW_CONCLUDED: "0e8a16",
}

LABEL_DESCRIPTIONS = {
    WORKFLOW_NEW: "Pipeline: issue nova, ainda não revisada",
    WORKFLOW_REVIEWING: "Pipeline: DEILE revisando (lock)",
    WORKFLOW_REVIEWED: "Pipeline: revisada, pronta para implementação",
    WORKFLOW_IMPLEMENTING: "Pipeline: Claude Code implementando (lock)",
    WORKFLOW_PR: "Pipeline: PR aberta",
    WORKFLOW_DONE: "Pipeline: concluída",
    REVIEW_PENDING: "Pipeline: PR aguardando revisão",
    REVIEW_IN_PROGRESS: "Pipeline: PR em revisão (lock)",
    REVIEW_CONCLUDED: "Pipeline: PR revisada/mergeada",
}


def is_batch_label(label: str) -> bool:
    """Return True if `label` is a per-task lock marker (``~batch:<sha>``)."""
    return label.startswith(BATCH_LABEL_PREFIX)


def make_batch_label(batch_id: str) -> str:
    return f"{BATCH_LABEL_PREFIX}{batch_id}"


def batch_id_from_label(label: str) -> str:
    if not is_batch_label(label):
        raise ValueError(f"not a batch label: {label!r}")
    return label[len(BATCH_LABEL_PREFIX):]
