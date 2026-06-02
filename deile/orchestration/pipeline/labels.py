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

import re
from typing import Iterable, Optional

# Priority labels ---------------------------------------------------------
# ``~prioridade:N`` — N is an integer ≥ 0, 0 = maximum urgency.
# Applied manually by the stakeholder on GitHub to influence pipeline ordering.
PRIORITY_LABEL_PREFIX = "~prioridade:"
PRIORITY_0 = "~prioridade:0"
PRIORITY_1 = "~prioridade:1"
PRIORITY_2 = "~prioridade:2"
PRIORITY_3 = "~prioridade:3"
PRIORITY_LABELS = (PRIORITY_0, PRIORITY_1, PRIORITY_2, PRIORITY_3)

_PRIORITY_LABEL_RE = re.compile(r"^~prioridade:(\d+)$")


def parse_priority_from_labels(labels: Iterable[str]) -> Optional[int]:
    """Return the priority N from the first ``~prioridade:N`` label found.

    Lower N = more urgent (0 = maximum). Returns ``None`` when no priority
    label is present — the caller should treat this as "minimum priority"
    and place the item after all prioritized ones.
    """
    for lb in labels:
        m = _PRIORITY_LABEL_RE.match(lb)
        if m:
            return int(m.group(1))
    return None


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

# Follow-ups handling -----------------------------------------------------
# Marker label posted by the pipeline after processing follow-ups on a merged
# PR, to enforce idempotency across ticks of the standalone follow_ups stage.
FOLLOW_UPS_PROCESSED = "~follow_ups:processed"

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

#: Comment-routing truth table (issue #442). A comment on an OPEN issue carrying
#: one of these ``~workflow:*`` states is SAFE to defer to the gate: each state
#: has an active worker dispatch whose brief re-reads the issue comments
#: (critique/refine/implement/resume — see ``briefs.py``), so a new comment is
#: picked up on the next dispatch without spawning a parallel one-shot.
GATE_REDISPATCHES_COMMENT = frozenset((
    WORKFLOW_NEW,
    WORKFLOW_REVIEWING,
    WORKFLOW_REFINING,
    WORKFLOW_ARCHITECTURE,
    WORKFLOW_REVIEWED,
    WORKFLOW_IMPLEMENTING,
))

#: Terminal pipeline states for an issue: NO stage selects an issue carrying one
#: of these (they are explicitly excluded in implement/resume/reconcile), so its
#: comments are NEVER re-read by a worker dispatch. A comment on such an issue
#: must be routed to a one-shot handler, NOT silently deferred to a gate that
#: will never run — the issue #442 limbo bug. ``WORKFLOW_WAITING`` is handled
#: separately (the comment lifts the pause), so it is in neither set; together
#: with these two sets it partitions all of ``WORKFLOW_LABELS``.
GATE_TERMINAL_NO_REDISPATCH = frozenset((
    WORKFLOW_PR,
    WORKFLOW_DECOMPOSED,
    WORKFLOW_BLOCKED,
))

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

# Label color palette. Each name encodes the semantic role on the timeline so
# changing the palette is a 1-line edit (vs. a hex-grep across the dict) and
# the meaning is visible in the LABEL_COLORS dict below.
_COLOR_GREEN_PROGRESS = "0e8a16"   # active forward progress (new / pending / concluded)
_COLOR_YELLOW_LOCK = "fbca04"      # transient lock state (reviewing / implementing)
_COLOR_LAVENDER_REFINE = "d4c5f9"  # refinement bucket (intent refining + ad-hoc)
_COLOR_ORANGE_WAITING = "d93f0b"   # paused, waiting on a human decision
_COLOR_PURPLE_REVIEWED = "5319e7"  # scope clear, ready to dispatch
_COLOR_BLUE_PR = "0052cc"          # PR opened (terminal pipeline state pre-merge)
_COLOR_BLUE_DECOMPOSED = "1d76db"  # intent decomposed (epic open)
_COLOR_RED_BLOCKED = "b60205"      # hard block, excluded from auto-resume
_COLOR_LIGHT_BLUE_DONE = "c5def5"  # mention already processed (sticky marker)
_COLOR_RED_HOT = "b60205"            # priority 0 — critical / maximum urgency
_COLOR_ORANGE_HIGH = "d93f0b"        # priority 1 — high
_COLOR_YELLOW_MEDIUM = "fbca04"      # priority 2 — medium
_COLOR_GREEN_LOW = "0e8a16"          # priority 3 — low

LABEL_COLORS = {
    WORKFLOW_NEW: _COLOR_GREEN_PROGRESS,
    WORKFLOW_REVIEWING: _COLOR_YELLOW_LOCK,
    WORKFLOW_REFINING: _COLOR_LAVENDER_REFINE,
    WORKFLOW_ARCHITECTURE: _COLOR_YELLOW_LOCK,
    WORKFLOW_WAITING: _COLOR_ORANGE_WAITING,
    WORKFLOW_REVIEWED: _COLOR_PURPLE_REVIEWED,
    WORKFLOW_IMPLEMENTING: _COLOR_YELLOW_LOCK,
    WORKFLOW_PR: _COLOR_BLUE_PR,
    WORKFLOW_DECOMPOSED: _COLOR_BLUE_DECOMPOSED,
    WORKFLOW_BLOCKED: _COLOR_RED_BLOCKED,
    REVIEW_PENDING: _COLOR_GREEN_PROGRESS,
    REVIEW_IN_PROGRESS: _COLOR_YELLOW_LOCK,
    REVIEW_CONCLUDED: _COLOR_GREEN_PROGRESS,
    MENTION_DONE: _COLOR_LIGHT_BLUE_DONE,
    REFINAR: _COLOR_LAVENDER_REFINE,
    PRIORITY_0: _COLOR_RED_HOT,
    PRIORITY_1: _COLOR_ORANGE_HIGH,
    PRIORITY_2: _COLOR_YELLOW_MEDIUM,
    PRIORITY_3: _COLOR_GREEN_LOW,
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
    REFINAR: "Pipeline: escopo vago — precisa de refinamento antes de avançar (humano pode aplicar à mão)",
    PRIORITY_0: "Pipeline: prioridade crítica — processar antes de tudo (0 = máxima urgência)",
    PRIORITY_1: "Pipeline: prioridade alta",
    PRIORITY_2: "Pipeline: prioridade média",
    PRIORITY_3: "Pipeline: prioridade baixa",
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


# --- Attempt label (issue #309 fase 3.5 — reaper) -------------------------
# Reaper incrementa ``~attempt:<N>`` ao re-claim uma PR/issue stuck. Quando
# ``N >= reaper_max_attempts`` (default 3), bloqueia em vez de liberar.
ATTEMPT_LABEL_PREFIX = "~attempt:"
_ATTEMPT_LABEL_RE = re.compile(r"^~attempt:(\d+)$")


def is_attempt_label(label: str) -> bool:
    return bool(_ATTEMPT_LABEL_RE.match(label))


def make_attempt_label(n: int) -> str:
    return f"{ATTEMPT_LABEL_PREFIX}{n}"


def parse_attempt_label(label: str) -> int:
    """Return N from ``~attempt:N``. Raises ValueError se não-attempt."""
    m = _ATTEMPT_LABEL_RE.match(label)
    if not m:
        raise ValueError(f"not an attempt label: {label!r}")
    return int(m.group(1))


def current_attempt_from_labels(labels) -> int:
    """Maior valor N entre labels ~attempt:N do conjunto (0 se ausentes)."""
    nums = []
    for lb in labels or ():
        m = _ATTEMPT_LABEL_RE.match(lb)
        if m:
            nums.append(int(m.group(1)))
    return max(nums) if nums else 0
