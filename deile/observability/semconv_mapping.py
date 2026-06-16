"""Mapeamento de atributos DEILE-local para OTel SemConv vcs.* — issue #456.

Implementa dual-emit não-destrutivo: attrs DEILE-local preservados intactos;
attrs ``vcs.*`` adicionados ao mesmo span já aberto por ``dispatch_export.py``.

Spec de referência: https://opentelemetry.io/docs/specs/semconv/registry/attributes/vcs/
Versão pinada: 1.27.0 (consistente com ``opentelemetry-api>=1.27.0`` em pyproject.toml).

Regra crítica: nenhuma leitura de env direta — config chega via
``ObservabilityConfig`` passado por ``dispatch_export.py``.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, Dict

if TYPE_CHECKING:
    from opentelemetry.trace import Span

__all__ = ["apply_semconv_attrs"]

# ── URL normalisation ─────────────────────────────────────────────────────

_SSH_RE = re.compile(r"^git@(?P<host>[^:]+):(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$")
_HTTPS_RE = re.compile(
    r"^https?://(?P<host>[^/]+)/(?P<owner>[^/]+)/(?P<repo>.+?)(?:\.git)?$"
)


def _normalize_repo_url(raw: str) -> str:
    """Converte SCP/SSH ou HTTPS com .git para URL canônica HTTPS sem .git.

    Forge-agnóstica: a transformação depende só da forma da URL (SCP/SSH vs
    HTTPS, sufixo ``.git``), nunca do host — vale para qualquer forge.

    Exemplos (``forge.example`` = host genérico, placeholder de qualquer forge):
      git@forge.example:owner/repo.git    → https://forge.example/owner/repo
      https://forge.example/owner/repo.git → https://forge.example/owner/repo
      https://forge.example/owner/repo     → https://forge.example/owner/repo (idempotente)
    """
    s = (raw or "").strip()
    if not s:
        return s

    m = _SSH_RE.match(s)
    if m:
        return f"https://{m.group('host')}/{m.group('owner')}/{m.group('repo')}"

    m = _HTTPS_RE.match(s)
    if m:
        return f"https://{m.group('host')}/{m.group('owner')}/{m.group('repo')}"

    return s


# ── mapping table ─────────────────────────────────────────────────────────


def _build_vcs_attrs(attrs: Dict[str, Any]) -> Dict[str, Any]:
    """Deriva attrs ``vcs.*`` a partir do dict de attrs DEILE-local."""
    out: Dict[str, Any] = {}

    branch = attrs.get("deile.git.branch")
    if branch is not None:
        out["vcs.ref.head.name"] = branch

    sha = attrs.get("deile.git.sha")
    if sha is not None:
        out["vcs.ref.head.revision"] = sha

    repo = attrs.get("deile.git.repo") or attrs.get("deile.forge.repo")
    if repo is not None:
        out["vcs.repository.url"] = _normalize_repo_url(str(repo))

    pr_number = attrs.get("deile.forge.pr_number")
    if pr_number is not None:
        out["vcs.change.id"] = str(pr_number)

    pr_state = attrs.get("deile.forge.status")
    if pr_state is not None:
        out["vcs.change.state"] = pr_state

    return out


# ── public API ────────────────────────────────────────────────────────────


def apply_semconv_attrs(span: "Span", attrs: Dict[str, Any]) -> None:
    """Adiciona attrs SemConv ``vcs.*`` ao span já aberto, sem modificar attrs DEILE-local.

    Chamado por ``dispatch_export.py`` condicionado a ``config.is_semconv_enabled``.
    Falha silenciosamente — nunca propaga exceção ao caller.
    """
    try:
        vcs_attrs = _build_vcs_attrs(attrs)
        for key, value in vcs_attrs.items():
            span.set_attribute(key, value)
    except Exception:  # noqa: BLE001 — semconv mapping never breaks dispatch
        pass
