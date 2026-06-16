"""Parse a forge URL into a canonical :class:`ForgeUrl`.

Reads URLs like:

- ``https://github.com/owner/repo/issues/42``           (GH issue)
- ``https://github.com/owner/repo/pull/77``             (GH PR)
- ``https://gitlab.com/group/project/-/issues/3``       (GL issue)
- ``https://gitlab.com/group/sub/project/-/merge_requests/9``  (GL nested MR)
- ``https://ghe.empresa.com/team/svc/pull/1``           (GHES PR — host override)
- ``https://gitlab.empresa.com/x/y/-/merge_requests/5`` (self-hosted GL)

The host whitelists come from ``settings`` so an operator can plug a
custom GHES / self-hosted GitLab host without editing the parser. Returns
``None`` for any URL it cannot confidently classify — never guesses.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Optional
from urllib.parse import urlparse

from deile.orchestration.forge.base import ForgeKind

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ForgeUrl:
    """A parsed forge URL.

    ``target_kind`` is normalised to ``'issue'`` or ``'pr'`` — the same
    vocabulary the pipeline uses internally, even though GitLab calls them
    "merge requests".
    """

    kind: ForgeKind
    host: str
    project_path: str
    target_kind: Literal["issue", "pr"]
    number: int


# GitHub: ``/<owner>/<repo>/{issues|pull}/<n>``. Owner/repo segments use the
# documented GitHub charset (alnum, dot, underscore, hyphen). The trailing
# fragment (e.g. ``#issuecomment-...``) is tolerated.
_GH_PATH_RE = re.compile(
    r"\A/(?P<project>[A-Za-z0-9._-]+/[A-Za-z0-9._-]+)/(?P<kind>issues|pull)/(?P<n>\d+)"
)

# GitLab: ``/<group>/(<subgroup>/)*<project>/-/{issues|merge_requests}/<n>``.
# The ``/-/`` is GitLab's canonical separator between path and resource.
_GL_PATH_RE = re.compile(
    r"\A/(?P<project>[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+)"
    r"/-/(?P<kind>issues|merge_requests)/(?P<n>\d+)"
)


def _is_github_host(host: str, *, extra_hosts: tuple = ()) -> bool:
    """True when *host* looks like a GitHub host.

    Cloud is ``github.com``; GHES uses a custom host the operator declares
    via ``DEILE_GITHUB_HOST``. The check is exact-match to avoid mistaking
    ``notgithub.com`` for GitHub.
    """
    return host == "github.com" or host in extra_hosts


def _is_gitlab_host(host: str, *, extra_hosts: tuple = ()) -> bool:
    """True when *host* looks like a GitLab host.

    Cloud is ``gitlab.com``; self-hosted instances declare via
    ``DEILE_GITLAB_HOST``. Treats ``gitlab.<rest>`` patterns conservatively:
    only matches the canonical ``gitlab.com`` automatically — anything else
    must be declared, so a typo never misroutes.
    """
    return host == "gitlab.com" or host in extra_hosts


def parse_forge_url(
    url: str,
    *,
    github_hosts: tuple = (),
    gitlab_hosts: tuple = (),
) -> Optional[ForgeUrl]:
    """Parse *url* into a :class:`ForgeUrl`, or return ``None`` on no match.

    ``github_hosts``/``gitlab_hosts`` are extra hostnames declared by the
    operator (typically read from settings) — added on top of the cloud
    defaults. The parser is intentionally strict: an unknown host returns
    ``None`` instead of guessing, so the caller can fail fast.
    """
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.strip())
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    host = parsed.netloc.lower()

    if _is_github_host(host, extra_hosts=github_hosts):
        m = _GH_PATH_RE.match(parsed.path or "")
        if not m:
            return None
        target = "pr" if m.group("kind") == "pull" else "issue"
        return ForgeUrl(
            kind=ForgeKind.GITHUB,
            host=host,
            project_path=m.group("project"),
            target_kind=target,
            number=int(m.group("n")),
        )

    if _is_gitlab_host(host, extra_hosts=gitlab_hosts):
        m = _GL_PATH_RE.match(parsed.path or "")
        if not m:
            return None
        target = "pr" if m.group("kind") == "merge_requests" else "issue"
        return ForgeUrl(
            kind=ForgeKind.GITLAB,
            host=host,
            project_path=m.group("project"),
            target_kind=target,
            number=int(m.group("n")),
        )

    return None


# Compatibility alias: every existing call site in the pipeline used a
# hardcoded ``_PR_URL_RE`` regex tied to ``github.com``. The forge layer
# exposes :func:`find_first_pr_url` to scan a free-text block for a PR/MR
# URL — useful in implementer-output parsing and notifier rendering.
_URL_SCAN_RE = re.compile(
    r"https?://[A-Za-z0-9.\-:]+/[^\s\"'<>]+",
    re.IGNORECASE,
)


def find_first_pr_url(
    text: str,
    *,
    github_hosts: tuple = (),
    gitlab_hosts: tuple = (),
) -> Optional[str]:
    """Return the first URL in *text* that resolves to a PR or MR.

    Used by stages.py to detect the agent's reported PR URL in a free-text
    final answer. Works for both GitHub and GitLab without the legacy
    ``r"https://github.com/.+/pull/\\d+"`` hardcoding.
    """
    if not text:
        return None
    for match in _URL_SCAN_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);:!?")
        forge_url = parse_forge_url(
            candidate,
            github_hosts=github_hosts,
            gitlab_hosts=gitlab_hosts,
        )
        if forge_url is not None and forge_url.target_kind == "pr":
            return candidate
    return None


def find_last_pr_url(
    text: str,
    *,
    github_hosts: tuple = (),
    gitlab_hosts: tuple = (),
) -> Optional[str]:
    """Return the **last** PR/MR URL found in *text*.

    Same semantics as the legacy ``_extract_pr_url`` helper in
    :mod:`deile.orchestration.pipeline.stages`: the agent often prints
    example URLs early in its output (in a brief recap of the request)
    and the *real* PR/MR URL on the final line — so the caller wants the
    last one, not the first.
    """
    if not text:
        return None
    last: Optional[str] = None
    for match in _URL_SCAN_RE.finditer(text):
        candidate = match.group(0).rstrip(".,);:!?")
        forge_url = parse_forge_url(
            candidate,
            github_hosts=github_hosts,
            gitlab_hosts=gitlab_hosts,
        )
        if forge_url is not None and forge_url.target_kind == "pr":
            last = candidate
    return last


__all__ = [
    "ForgeUrl",
    "parse_forge_url",
    "find_first_pr_url",
    "find_last_pr_url",
]
