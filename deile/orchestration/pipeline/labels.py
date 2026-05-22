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

from typing import Iterable, Optional

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
# Refinement gate (issue #257). After critique judges an issue's scope POOR it
# enters the refinement loop, in a type-specific state: an ``intent`` (analyst,
# product/discovery work) goes to ``em_refinamento``; a ``feature``/``bug``/
# ``refactor`` (architect/debugger, design work) goes to ``em_arquitetura`` (the
# yellow "architecture phase" label, removed when implementation starts). They
# are the SAME slot, picked by type — see ``refine_workflow_state``.
WORKFLOW_REFINING = "~workflow:em_refinamento"
WORKFLOW_ARCHITECTURE = "~workflow:em_arquitetura"
# Momentary PAUSE overlay (issue #257): the refiner hit a gap/scope decision that
# only the stakeholder (issue author) can resolve. It posted 2-3 suggestions and
# assigned the author. This label COEXISTS with the refine state (em_refinamento/
# em_arquitetura) and is OBSERVED to pause the refine stage — the human removes it
# (after commenting their decision) to resume refinement. ``refinar`` stays set.
WORKFLOW_WAITING = "~workflow:aguardando_stakeholder"
WORKFLOW_REVIEWED = "~workflow:revisada"
WORKFLOW_IMPLEMENTING = "~workflow:em_implementacao"
WORKFLOW_PR = "~workflow:em_pr"
# Terminal state for an ``intent`` that the pipeline DECOMPOSED into one or more
# derived ``feature``/``bug``/``refactor`` issues. The intent stays OPEN as a
# tracking epic (the human closes it manually — see the issue template), but
# this label drops it out of every stage's candidate set so it is never
# re-decomposed. It is a ``~workflow:*`` state, so the defense-in-depth
# ``startswith("~")`` guard in the classify stage also skips it.
WORKFLOW_DECOMPOSED = "~workflow:decomposta"
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

# Refinement gate ---------------------------------------------------------
# Universal "needs refinement" flag (no ``~`` prefix on purpose: a human applies
# it by hand too). The critique stage adds it to a ``~workflow:nova`` issue whose
# scope is judged POOR; the refine stage acts on ANY open issue carrying it
# (regardless of type), improves the body toward the matching ``.github`` template
# and removes it. No code/decomposition ever runs while an issue is poor — it must
# pass critique first. A human can add ``refinar`` to force a refinement pass.
REFINAR = "refinar"

# Issue type labels (mirror ``.github/ISSUE_TEMPLATE/*``) -----------------
# These are PROJECT labels (no ``~`` prefix). They route an issue to the right
# refinement lens (persona) and the matching template during critique/refine.
TYPE_INTENT = "intent"
TYPE_FEATURE = "feature"
TYPE_BUG = "bug"
TYPE_REFACTOR = "refactor"
ISSUE_TYPE_LABELS = (TYPE_INTENT, TYPE_FEATURE, TYPE_BUG, TYPE_REFACTOR)

# Which refinement persona owns each type (critique + refine; decompose is
# always ``architect``). An ``intent`` is product/discovery work (analyst), a
# ``feature``/``refactor`` is architectural deepening (architect), a ``bug`` is
# code investigation (debugger).
TYPE_TO_PERSONA = {
    TYPE_INTENT: "analyst",
    TYPE_FEATURE: "architect",
    TYPE_BUG: "debugger",
    TYPE_REFACTOR: "architect",
}
# The matching ``.github/ISSUE_TEMPLATE`` file for each type.
TYPE_TO_TEMPLATE = {
    TYPE_INTENT: "intent.md",
    TYPE_FEATURE: "feature_request.md",
    TYPE_BUG: "bug_report.md",
    TYPE_REFACTOR: "refactor_proposal.md",
}
# Title prefix each template enforces (``title: '[FEATURE] '`` etc.) — the refiner
# normalizes the issue title to start with this tag.
TYPE_TO_TITLE_PREFIX = {
    TYPE_INTENT: "[INTENT]",
    TYPE_FEATURE: "[FEATURE]",
    TYPE_BUG: "[BUG]",
    TYPE_REFACTOR: "[REFACTOR]",
}
# ``enhancement`` is the GitHub-conventional feature label and is treated as an
# alias of ``feature`` (it is already accepted by ``classifiable_labels``).
_TYPE_ALIASES = {"enhancement": TYPE_FEATURE}


def issue_type_from_labels(labels: Iterable[str]) -> Optional[str]:
    """Return the canonical issue type (``intent``/``feature``/``bug``/``refactor``).

    Checks in priority order so an issue carrying more than one type label
    resolves deterministically: ``intent`` (decomposes) wins, then ``bug``,
    ``refactor`` and finally ``feature`` (incl. the ``enhancement`` alias).
    Returns ``None`` when no recognized type label is present.
    """
    labset = set(labels)
    for t in (TYPE_INTENT, TYPE_BUG, TYPE_REFACTOR, TYPE_FEATURE):
        if t in labset:
            return t
    for alias, canonical in _TYPE_ALIASES.items():
        if alias in labset:
            return canonical
    return None


def persona_for_type(issue_type: Optional[str]) -> str:
    """Persona that critiques/refines an issue of *issue_type* (default developer)."""
    return TYPE_TO_PERSONA.get(issue_type or "", "developer")


def template_for_type(issue_type: Optional[str]) -> Optional[str]:
    """Matching ``.github/ISSUE_TEMPLATE`` filename for *issue_type* (or None)."""
    return TYPE_TO_TEMPLATE.get(issue_type or "")


def title_prefix_for_type(issue_type: Optional[str]) -> str:
    """Title tag the template enforces (``[FEATURE]`` etc.); empty if unknown."""
    return TYPE_TO_TITLE_PREFIX.get(issue_type or "", "")


# Distributed lock --------------------------------------------------------
BATCH_LABEL_PREFIX = "~batch:"

WORKFLOW_LABELS = (
    WORKFLOW_NEW,
    WORKFLOW_REVIEWING,
    WORKFLOW_REFINING,
    WORKFLOW_ARCHITECTURE,
    WORKFLOW_WAITING,
    WORKFLOW_REVIEWED,
    WORKFLOW_IMPLEMENTING,
    WORKFLOW_PR,
    WORKFLOW_DECOMPOSED,
    WORKFLOW_BLOCKED,
)

#: The two refine states are the same logical step, chosen by issue type:
#: intent → em_refinamento (analyst); code types → em_arquitetura (architect/
#: debugger). ``refine_workflow_state`` is the single source of that mapping.
REFINE_WORKFLOW_STATES = (WORKFLOW_REFINING, WORKFLOW_ARCHITECTURE)


def refine_workflow_state(issue_type: Optional[str]) -> str:
    """Return the refine ``~workflow:`` state for *issue_type*.

    ``intent`` refines as product/discovery work (``em_refinamento``); every
    code type (feature/bug/refactor, incl. unknown) refines as design work
    (``em_arquitetura``, the yellow architecture-phase label).
    """
    return WORKFLOW_REFINING if issue_type == TYPE_INTENT else WORKFLOW_ARCHITECTURE

REVIEW_LABELS = (REVIEW_PENDING, REVIEW_IN_PROGRESS, REVIEW_CONCLUDED)

MENTION_LABELS = (MENTION_DONE,)

# Refinement gate: the only pipeline-created label here is ``refinar`` (the type
# labels are project-owned and created by the issue templates).
REFINE_LABELS = (REFINAR,)

LABEL_COLORS = {
    WORKFLOW_NEW: "0e8a16",
    WORKFLOW_REVIEWING: "fbca04",
    WORKFLOW_REFINING: "d4c5f9",
    WORKFLOW_ARCHITECTURE: "fbca04",
    WORKFLOW_WAITING: "d93f0b",
    WORKFLOW_REVIEWED: "5319e7",
    WORKFLOW_IMPLEMENTING: "fbca04",
    WORKFLOW_PR: "0052cc",
    WORKFLOW_DECOMPOSED: "1d76db",
    WORKFLOW_BLOCKED: "b60205",
    REVIEW_PENDING: "0e8a16",
    REVIEW_IN_PROGRESS: "fbca04",
    REVIEW_CONCLUDED: "0e8a16",
    MENTION_DONE: "c5def5",
    REFINAR: "d4c5f9",
}

LABEL_DESCRIPTIONS = {
    WORKFLOW_NEW: "Pipeline: issue nova, ainda não revisada",
    WORKFLOW_REVIEWING: "Pipeline: DEILE criticando escopo (lock)",
    WORKFLOW_REFINING: "Pipeline: intent em refinamento (analyst)",
    WORKFLOW_ARCHITECTURE: "Pipeline: feature/bug/refactor em arquitetura/design (architect/debugger) — sai ao implementar",
    WORKFLOW_WAITING: "Pipeline: aguardando decisão do stakeholder (pausa o refino) — humano comenta e remove para retomar",
    WORKFLOW_REVIEWED: "Pipeline: revisada, escopo claro — pronta para implementação/decomposição",
    WORKFLOW_IMPLEMENTING: "Pipeline: DEILE implementando (lock)",
    WORKFLOW_PR: "Pipeline: PR aberta",
    WORKFLOW_DECOMPOSED: "Pipeline: intent decomposta em issues derivadas (épico aberto) — humano fecha manualmente",
    WORKFLOW_BLOCKED: "Pipeline: bloqueada (sem progresso / impedimento / teto) — humano remove para desbloquear",
    REVIEW_PENDING: "Pipeline: PR aguardando revisão",
    REVIEW_IN_PROGRESS: "Pipeline: PR em revisão (lock)",
    REVIEW_CONCLUDED: "Pipeline: PR revisada/mergeada",
    MENTION_DONE: "Pipeline: menção/atribuição já processada — humano remove para reprocessar",
    REFINAR: "Pipeline: escopo pobre — precisa de refinamento antes de avançar (humano pode aplicar à mão)",
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
