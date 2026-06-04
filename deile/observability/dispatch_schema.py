"""Schema V1 dos eventos de dispatch para o adapter OTLP (Decisão #47).

Define atributos canônicos por família de evento como dataclasses ``frozen=True``.
``SCHEMA_VERSION`` é exposto como atributo de span ``deile.dispatch.schema_version``
em todo span emitido pelo adapter.

``test_dispatch_schema_drift.py`` falha se as chaves dos atributos aqui
divergirem do que ``dispatch_export`` efetivamente emite.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, ClassVar, Dict

__all__ = [
    "SCHEMA_VERSION",
    "ATTR_SCHEMA_VERSION",
    "ATTR_ROLE",
    "ATTR_POD",
    "DispatchReceivedAttrs",
    "DispatchModelResolvedAttrs",
    "DispatchProgressAttrs",
    "DispatchToolBurstAttrs",
    "DispatchCompletedAttrs",
    "DispatchFailedAttrs",
    "GitCommitAttrs",
    "GitPushAttrs",
    "ForgePrOpenAttrs",
    "ForgePrReviewAttrs",
    "get_pod_metadata",
]

SCHEMA_VERSION = "1.0.0"
ATTR_SCHEMA_VERSION = "deile.dispatch.schema_version"
ATTR_ROLE = "deile.role"
ATTR_POD = "deile.pod"


def get_pod_metadata() -> Dict[str, str]:
    """Lê DEILE_ROLE e HOSTNAME do environment para metadados do pod."""
    return {
        "role": os.environ.get("DEILE_ROLE", ""),
        "pod": os.environ.get("HOSTNAME", ""),
    }


def _schema_keys(instance: Any) -> frozenset:
    """Retorna o conjunto de chaves de atributo OTLP de uma instância de schema."""
    return frozenset(instance.to_span_attrs().keys())


@dataclass(frozen=True)
class DispatchReceivedAttrs:
    """Atributos do span root ``deile.dispatch`` ao abrir (evento ``dispatch.received``)."""

    task_id: str = ""
    session_id: str = ""
    model: str = ""
    branch: str = ""

    SPAN_NAME: ClassVar[str] = "deile.dispatch"
    EVENT_NAME: ClassVar[str] = "dispatch.received"

    def to_span_attrs(self) -> Dict[str, str]:
        return {
            "deile.dispatch.task_id": self.task_id,
            "deile.dispatch.session_id": self.session_id,
            "deile.dispatch.model": self.model,
            "deile.dispatch.branch": self.branch,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return _schema_keys(cls())


@dataclass(frozen=True)
class DispatchModelResolvedAttrs:
    """Atributos do evento ``dispatch.model_resolved`` no span root."""

    model: str = ""

    EVENT_NAME: ClassVar[str] = "dispatch.model_resolved"

    def to_event_attrs(self) -> Dict[str, str]:
        return {
            "deile.dispatch.model": self.model,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return frozenset(cls().to_event_attrs().keys())


@dataclass(frozen=True)
class DispatchProgressAttrs:
    """Atributos do evento ``dispatch.progress`` no span root."""

    step: str = ""
    elapsed_s: float = 0.0

    EVENT_NAME: ClassVar[str] = "dispatch.progress"

    def to_event_attrs(self) -> Dict[str, Any]:
        return {
            "deile.dispatch.step": self.step,
            "deile.dispatch.elapsed_s": self.elapsed_s,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return frozenset(cls().to_event_attrs().keys())


@dataclass(frozen=True)
class DispatchToolBurstAttrs:
    """Atributos do evento ``dispatch.tool_burst`` no span root."""

    tools: str = ""
    count: int = 0

    EVENT_NAME: ClassVar[str] = "dispatch.tool_burst"

    def to_event_attrs(self) -> Dict[str, Any]:
        return {
            "deile.dispatch.tools": self.tools,
            "deile.dispatch.tool_count": self.count,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return frozenset(cls().to_event_attrs().keys())


@dataclass(frozen=True)
class DispatchCompletedAttrs:
    """Atributos do evento ``dispatch.completed`` ao fechar o span root com OK."""

    elapsed_s: float = 0.0
    outcome: str = ""

    EVENT_NAME: ClassVar[str] = "dispatch.completed"

    def to_event_attrs(self) -> Dict[str, Any]:
        return {
            "deile.dispatch.elapsed_s": self.elapsed_s,
            "deile.dispatch.outcome": self.outcome,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return frozenset(cls().to_event_attrs().keys())


@dataclass(frozen=True)
class DispatchFailedAttrs:
    """Atributos do evento ``dispatch.failed`` ao fechar o span root com ERROR."""

    reason: str = ""
    elapsed_s: float = 0.0

    EVENT_NAME: ClassVar[str] = "dispatch.failed"

    def to_event_attrs(self) -> Dict[str, Any]:
        return {
            "deile.dispatch.reason": self.reason,
            "deile.dispatch.elapsed_s": self.elapsed_s,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return frozenset(cls().to_event_attrs().keys())


@dataclass(frozen=True)
class GitCommitAttrs:
    """Atributos do child span ``git.commit``."""

    repo: str = ""
    sha: str = ""
    status: str = ""

    SPAN_NAME: ClassVar[str] = "git.commit"

    def to_span_attrs(self) -> Dict[str, str]:
        return {
            "deile.git.repo": self.repo,
            "deile.git.sha": self.sha,
            "deile.git.status": self.status,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return _schema_keys(cls())


@dataclass(frozen=True)
class GitPushAttrs:
    """Atributos do child span ``git.push``."""

    repo: str = ""
    branch: str = ""
    status: str = ""

    SPAN_NAME: ClassVar[str] = "git.push"

    def to_span_attrs(self) -> Dict[str, str]:
        return {
            "deile.git.repo": self.repo,
            "deile.git.branch": self.branch,
            "deile.git.status": self.status,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return _schema_keys(cls())


@dataclass(frozen=True)
class ForgePrOpenAttrs:
    """Atributos do child span ``forge.pr_open``."""

    repo: str = ""
    pr_number: int = 0
    status: str = ""

    SPAN_NAME: ClassVar[str] = "forge.pr_open"

    def to_span_attrs(self) -> Dict[str, Any]:
        return {
            "deile.forge.repo": self.repo,
            "deile.forge.pr_number": self.pr_number,
            "deile.forge.status": self.status,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return _schema_keys(cls())


@dataclass(frozen=True)
class ForgePrReviewAttrs:
    """Atributos do child span ``forge.pr_review``."""

    repo: str = ""
    pr_number: int = 0
    status: str = ""

    SPAN_NAME: ClassVar[str] = "forge.pr_review"

    def to_span_attrs(self) -> Dict[str, Any]:
        return {
            "deile.forge.repo": self.repo,
            "deile.forge.pr_number": self.pr_number,
            "deile.forge.status": self.status,
        }

    @classmethod
    def expected_keys(cls) -> frozenset:
        return _schema_keys(cls())
