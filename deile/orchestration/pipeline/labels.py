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
# Only the states actually transitioned by the pipeline live here.
# Stage 0 sets ``WORKFLOW_NEW``, Stage 1 transitions to ``WORKFLOW_REVIEWING``
# and then ``WORKFLOW_REVIEWED``. Stage 2 *claims* the issue by transitioning
# ``WORKFLOW_REVIEWED`` → ``WORKFLOW_IMPLEMENTING`` BEFORE doing any work — this
# is the lock that stops the same issue from being picked up twice (the
# candidate query only returns ``WORKFLOW_REVIEWED`` issues, so a claimed one
# drops out of the set) — then moves it to ``WORKFLOW_PR`` on success. An
# implementation that fails or opens no PR stays parked in
# ``WORKFLOW_IMPLEMENTING`` (out of every stage's candidate set) until a human
# moves it back to ``WORKFLOW_REVIEWED``. There is no ``concluida`` close state
# — the PR is the final state from the issue's perspective — so it is absent.
WORKFLOW_NEW = "~workflow:nova"
WORKFLOW_REVIEWING = "~workflow:em_revisao"
WORKFLOW_REVIEWED = "~workflow:revisada"
WORKFLOW_IMPLEMENTING = "~workflow:em_implementacao"
WORKFLOW_PR = "~workflow:em_pr"
# Resume feature (issue #254): a serious, non-continuable block. The agent
# declared ``BLOQUEADO: <motivo>``, the progress guard saw 0 substantive
# progress between attempts, or the attempt/budget ceiling was hit. The issue
# KEEPS ``WORKFLOW_IMPLEMENTING`` so it never re-enters the implement queue, and
# the extra ``WORKFLOW_BLOCKED`` excludes it from the auto-resume selection too.
# A human removes this label to unblock (which lets auto-resume pick it up
# again on the next free tick).
WORKFLOW_BLOCKED = "~workflow:bloqueada"

# PR workflow -------------------------------------------------------------
REVIEW_PENDING = "~review:pendente"
REVIEW_IN_PROGRESS = "~review:em_andamento"
REVIEW_CONCLUDED = "~review:concluida"

# Mention handling --------------------------------------------------------
# Sticky "already handled" marker for the mention stage (issue #253 follow-up).
# Comment mentions are deduplicated by the timestamp cursor, but the STICKY
# triggers (assignee / requested-reviewer / body-mention) describe a state that
# does not change tick-to-tick — so without a marker they re-fire on EVERY tick,
# re-dispatching the same implement/review forever (the duplicate-DM storm bug
# class). After a successful mention dispatch whose group carried a sticky
# trigger, the target gets this label and is excluded from subsequent sticky
# polls. A NEW comment still re-triggers (comments ignore this label and are
# governed by the cursor). A human removes it to force a re-handle.
MENTION_DONE = "~mention:processado"

# Distributed lock --------------------------------------------------------
BATCH_LABEL_PREFIX = "~batch:"

WORKFLOW_LABELS = (
    WORKFLOW_NEW,
    WORKFLOW_REVIEWING,
    WORKFLOW_REVIEWED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_PR,
    WORKFLOW_BLOCKED,
)

REVIEW_LABELS = (REVIEW_PENDING, REVIEW_IN_PROGRESS, REVIEW_CONCLUDED)

MENTION_LABELS = (MENTION_DONE,)

LABEL_COLORS = {
    WORKFLOW_NEW: "0e8a16",
    WORKFLOW_REVIEWING: "fbca04",
    WORKFLOW_REVIEWED: "5319e7",
    WORKFLOW_IMPLEMENTING: "fbca04",
    WORKFLOW_PR: "0052cc",
    WORKFLOW_BLOCKED: "b60205",
    REVIEW_PENDING: "0e8a16",
    REVIEW_IN_PROGRESS: "fbca04",
    REVIEW_CONCLUDED: "0e8a16",
    MENTION_DONE: "c5def5",
}

LABEL_DESCRIPTIONS = {
    WORKFLOW_NEW: "Pipeline: issue nova, ainda não revisada",
    WORKFLOW_REVIEWING: "Pipeline: DEILE revisando (lock)",
    WORKFLOW_REVIEWED: "Pipeline: revisada, pronta para implementação",
    WORKFLOW_IMPLEMENTING: "Pipeline: DEILE implementando (lock)",
    WORKFLOW_PR: "Pipeline: PR aberta",
    WORKFLOW_BLOCKED: "Pipeline: bloqueada (sem progresso / impedimento / teto) — humano remove para desbloquear",
    REVIEW_PENDING: "Pipeline: PR aguardando revisão",
    REVIEW_IN_PROGRESS: "Pipeline: PR em revisão (lock)",
    REVIEW_CONCLUDED: "Pipeline: PR revisada/mergeada",
    MENTION_DONE: "Pipeline: menção/atribuição já processada — humano remove para reprocessar",
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
