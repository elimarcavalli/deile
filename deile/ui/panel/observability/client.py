"""HTTP client for the cluster observability endpoints (issue #347).

This module is intentionally tiny — it owns no Rich, no asyncio loop, no
panel layout.  It maps endpoint URLs to Python dicts using ``aiohttp``,
shielding the panel renderer from network mistakes via short timeouts and
``ConnectionError``-style fallbacks (so a pipeline pod that is down does
not freeze the entire panel).

Three logical clients are wrapped by :class:`ClusterObservabilityClient`:

* :class:`PipelineStatusClient`        — talks to the deile-pipeline pod
* :class:`ClaudeWorkerSessionsClient`  — talks to the claude-worker pod
* :class:`WorkerSessionsClient`        — alias of the above, reserved for
                                         the deile-worker observability surface
                                         (kept for forward compatibility)

Each call returns either the parsed JSON body or an :class:`ApiError`
instance carrying the HTTP status and a short message — handlers never
raise to the caller.  The panel can render whichever is present.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Union

logger = logging.getLogger(__name__)


@dataclass
class ApiError:
    """Failure value returned by every method on the client.

    ``status`` is ``0`` for connection errors, ``-1`` for cancellation;
    everything else is the actual HTTP status.  ``message`` is short and
    intended for direct rendering in a footer row (no traceback dump).
    """

    status: int
    message: str
    detail: Optional[Dict[str, Any]] = None


JsonLike = Union[Dict[str, Any], List[Any]]
Reply = Union[JsonLike, ApiError]


class _BaseHTTPClient:
    """Shared aiohttp plumbing — short timeouts, single ClientSession reuse.

    The same instance is safe to use across the panel's refresh loop;
    creating a session per call (the naive path) leaks connections and
    spends the panel's polling budget on TCP handshakes.
    """

    DEFAULT_TIMEOUT_SECONDS = 3.0

    def __init__(
        self,
        base_url: str,
        *,
        bearer_token: Optional[str] = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        session: Optional["object"] = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.bearer_token = bearer_token
        self.timeout_seconds = timeout_seconds
        # ``session`` is the aiohttp.ClientSession — typed as object to
        # avoid eagerly importing aiohttp at module load (tests that only
        # exercise client logic via fake_aiohttp can skip the import).
        self._session = session

    async def _request(
        self, method: str, path: str, *, json_body: Optional[dict] = None
    ) -> Reply:
        url = f"{self.base_url}{path}"
        try:
            import aiohttp  # local import keeps module import cheap
        except ImportError as exc:  # pragma: no cover — aiohttp is a hard dep
            return ApiError(status=0, message=f"aiohttp unavailable: {exc}")

        headers = {}
        if self.bearer_token:
            headers["Authorization"] = f"Bearer {self.bearer_token}"

        timeout = aiohttp.ClientTimeout(total=self.timeout_seconds)
        own_session = self._session is None
        session = self._session or aiohttp.ClientSession(timeout=timeout)
        try:
            async with session.request(
                method,
                url,
                headers=headers,
                json=json_body,
            ) as resp:
                try:
                    body = await resp.json()
                except Exception:
                    body = {"raw": await resp.text()}
                if resp.status >= 400:
                    return ApiError(
                        status=resp.status,
                        message=f"{method} {path} -> {resp.status}",
                        detail=body if isinstance(body, dict) else None,
                    )
                return body
        except asyncio.TimeoutError:
            return ApiError(status=0, message=f"{method} {path}: timeout")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # broad: connection-refused, DNS, etc.
            return ApiError(status=0, message=f"{method} {path}: {exc}")
        finally:
            if own_session:
                await session.close()

    async def get(self, path: str) -> Reply:
        return await self._request("GET", path)

    async def post(self, path: str, *, json_body: Optional[dict] = None) -> Reply:
        return await self._request("POST", path, json_body=json_body)

    async def delete(self, path: str) -> Reply:
        return await self._request("DELETE", path)


class PipelineStatusClient(_BaseHTTPClient):
    """Wraps the deile-pipeline ``/v1/pipeline-status*`` endpoints."""

    async def get_status(self) -> Reply:
        return await self.get("/v1/pipeline-status")

    async def get_backlog(self) -> Reply:
        return await self.get("/v1/pipeline-status/backlog")

    async def get_recent(self, *, limit: Optional[int] = None) -> Reply:
        qs = f"?limit={limit}" if limit else ""
        return await self.get(f"/v1/pipeline-status/recent{qs}")

    async def get_ledger(self) -> Reply:
        return await self.get("/v1/pipeline-status/ledger")

    async def get_reaper_preview(self) -> Reply:
        return await self.get("/v1/pipeline-status/reaper-preview")

    async def force_tick(self) -> Reply:
        return await self.post("/v1/pipeline/force-tick")


class ClaudeWorkerSessionsClient(_BaseHTTPClient):
    """Wraps the claude-worker ``/v1/sessions*`` endpoints."""

    async def list_sessions(self) -> Reply:
        return await self.get("/v1/sessions")

    async def get_command(self, task_id: str) -> Reply:
        return await self.get(f"/v1/sessions/{task_id}/command")

    async def get_chat(self, task_id: str, *, tail: int = 50) -> Reply:
        return await self.get(f"/v1/sessions/{task_id}/chat?tail={tail}")

    async def get_stdout(self, task_id: str, *, tail_bytes: int = 8192) -> Reply:
        return await self.get(
            f"/v1/sessions/{task_id}/stdout?tail_bytes={tail_bytes}",
        )

    async def kill(self, task_id: str) -> Reply:
        confirm = f"yes-task-{task_id[:8]}"
        return await self.post(
            f"/v1/sessions/{task_id}/kill",
            json_body={"confirm": confirm},
        )

    async def cleanup(self, task_id: str) -> Reply:
        return await self.delete(f"/v1/sessions/{task_id}/cleanup")


WorkerSessionsClient = ClaudeWorkerSessionsClient
"""Alias reserved for future deile-worker observability parity."""


@dataclass
class ClusterObservabilityClient:
    """Composite client used by :mod:`deile.ui.panel.observability.screens`.

    The panel main loop reads each sub-client's responses concurrently via
    :func:`asyncio.gather` so a slow pipeline does not block the worker
    sessions view (and vice-versa).  Construction is explicit — each URL
    and bearer token comes from settings or env vars, no hardcoded
    defaults.
    """

    pipeline: PipelineStatusClient
    claude_worker: ClaudeWorkerSessionsClient

    @classmethod
    def from_endpoints(
        cls,
        *,
        pipeline_url: str,
        pipeline_token: Optional[str],
        claude_worker_url: str,
        claude_worker_token: Optional[str],
        timeout_seconds: float = _BaseHTTPClient.DEFAULT_TIMEOUT_SECONDS,
    ) -> "ClusterObservabilityClient":
        return cls(
            pipeline=PipelineStatusClient(
                pipeline_url,
                bearer_token=pipeline_token,
                timeout_seconds=timeout_seconds,
            ),
            claude_worker=ClaudeWorkerSessionsClient(
                claude_worker_url,
                bearer_token=claude_worker_token,
                timeout_seconds=timeout_seconds,
            ),
        )
