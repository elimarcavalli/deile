"""Vision tool — describe / interpret images using a cheap multimodal LLM.

Why this lives as a tool (not a provider feature):
- Most callers want a one-shot "what is in this image?" answer; promoting
  that into the message-list format of every provider is a large change
  for a small benefit. A tool gives the agent an explicit, auditable hook.
- The tool is provider-locked to **Gemini 2.5 Flash-Lite** today (cheapest
  vision-capable model on the roster: $0.10/$0.40 per 1M tokens, supports
  `image/jpeg`, `image/png`, `image/webp`, `image/gif`). When the operator
  sets `DEILE_VISION_MODEL`, that override wins.
- Accepts EITHER `image_url` (downloaded with httpx) OR `image_base64` +
  `mime_type` (raw bytes already in hand). Discord attachment URLs are public
  CDN links; no auth needed.

Security & limits:
- The downloader has a hard 10 MiB cap and a 15 s timeout (Discord's own
  per-attachment limits are more generous, but vision payload + base64
  inflation make this a sensible ceiling for one tool call).
- The tool routes through `PermissionManager` like any other tool; it
  does not need approval (read-only).
- Emits `AuditEvent(TOOL_EXECUTION)` on success, logging SHA8 of the image
  body (not the body itself) plus source type, mime, and byte-count.
"""

from __future__ import annotations

import asyncio
import base64
import ipaddress
import logging
import socket
from pathlib import Path
from typing import Any
from urllib.parse import unquote as _urlunquote
from urllib.parse import urlparse as _urlparse

import aiofiles
import httpx

from deile.core.exceptions import DEILEError, PathContainmentError
from deile.tools._hash_utils import sha8 as _sha8
from deile.tools._pipeline_paths import _assert_safe_root

from .base import (SecurityLevel, Tool, ToolCategory, ToolContext, ToolResult,
                   ToolSchema)

logger = logging.getLogger(__name__)

_DEFAULT_VISION_MODEL = "gemini-2.5-flash-lite"
_MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MiB
_DOWNLOAD_TIMEOUT_S = 15.0
_DNS_TIMEOUT_S = 5.0
_GEMINI_TIMEOUT_S = 60.0
_MAX_PROMPT_BYTES = 8192
_ALLOWED_MIME_PREFIXES = ("image/",)
_ALLOWED_VISION_MODELS: frozenset[str] = frozenset({
    "gemini-2.5-flash-lite",
    "gemini-2.5-flash",
    "gemini-2.0-flash-lite",
    "gemini-1.5-flash-8b",
    "gemini-1.5-flash",
})
_MAGIC_BYTES: dict[str, bytes] = {
    "image/png": b"\x89PNG",
    "image/jpeg": b"\xff\xd8\xff",
    # GIF and WebP have multi-part or variant magic checks handled explicitly in
    # _validate_magic_bytes; they are not listed here.
}
_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_GEMINI_SUPPORTED_MIMES = frozenset(_EXT_TO_MIME.values())
# IPv6 ranges that Python's ipaddress reports as is_global=True but must be blocked for SSRF.
# fec0::/10 is the deprecated site-local range (RFC 3879); Python <=3.13 lacks a dedicated
# is_site_local attribute and does not exclude it from is_global.
_SSRF_BLOCKED_IPV6_NETS: tuple[ipaddress.IPv6Network, ...] = (
    ipaddress.IPv6Network("fec0::/10"),
)

def _sanitise_url_for_audit(url: str) -> str:
    """Strip userinfo, query, and fragment from URL before writing to audit log."""
    p = _urlparse(url)
    netloc = (p.hostname or "") + (f":{p.port}" if p.port else "")
    return p._replace(netloc=netloc, params="", query="", fragment="").geturl()


_DEFAULT_PROMPT = (
    "Descreva exatamente o que está nesta imagem em 2-4 linhas em português. "
    "Inclua: objetos visíveis, texto legível (transcreva), pessoas (sem identificar), "
    "cores predominantes e contexto geral. Sem floreios."
)


def _resolve_vision_model() -> str:
    from deile.config.settings import get_settings

    model = get_settings().vision_model or _DEFAULT_VISION_MODEL
    if model not in _ALLOWED_VISION_MODELS:
        logger.warning("DEILE_VISION_MODEL %r not in allowlist; using default", model)
        return _DEFAULT_VISION_MODEL
    return model


class VisionToolError(DEILEError):
    """Typed error so the tool returns a consistent error_code."""

    def __init__(self, code: str, message: str):
        super().__init__(message, error_code=code)
        self.code = code


class VisionDescribeImageTool(Tool):
    """Describe an image with a vision-capable LLM (Gemini Flash-Lite default)."""

    def __init__(self) -> None:
        super().__init__(
            schema=ToolSchema(
                name="vision_describe_image",
                description=(
                    "Interpret/describe an image using a vision-capable LLM (Gemini "
                    "Flash-Lite by default). Accepts EITHER image_url (https URL the "
                    "tool will download) OR image_base64 + mime_type (raw bytes already "
                    "in hand). Optional `prompt` overrides the default 'describe in PT'. "
                    "Use this when the user attaches an image to a Discord message OR "
                    "passes an image URL/path explicitly. Returns the model's textual "
                    "answer plus the SHA8 of the image bytes for audit."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "image_url": {
                            "type": "string",
                            "description": "http(s) URL to fetch the image from (e.g. Discord CDN URL). HTTPS strongly preferred; HTTP accepted for internal/test use.",
                        },
                        "image_path": {
                            "type": "string",
                            "description": "Local filesystem path to an image (e.g. 'docs/img/banner.png'). The tool reads the bytes itself; never use bash for this.",
                        },
                        "image_base64": {
                            "type": "string",
                            "description": "Base64-encoded image bytes (RFC 4648). Use with mime_type.",
                        },
                        "mime_type": {
                            "type": "string",
                            "description": "Image MIME type (image/jpeg, image/png, image/webp, image/gif). Required with image_base64; auto-detected from extension when using image_path.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "Optional instruction passed to the vision model. Defaults to a Portuguese description prompt.",
                        },
                        "model": {
                            "type": "string",
                            "description": "Optional override of the vision model (default gemini-2.5-flash-lite).",
                        },
                    },
                },
                required=[],
                security_level=SecurityLevel.MODERATE,
                category=ToolCategory.OTHER,
            )
        )


    async def execute(self, context: ToolContext) -> ToolResult:
        args = dict(context.parsed_args or {})
        url = (args.get("image_url") or "").strip() or None
        path = (args.get("image_path") or "").strip() or None
        b64 = (args.get("image_base64") or "").strip() or None
        mime = (args.get("mime_type") or "").strip() or None
        prompt = (args.get("prompt") or _DEFAULT_PROMPT).strip()
        if len(prompt.encode("utf-8")) > _MAX_PROMPT_BYTES:
            return ToolResult.error_result(
                f"prompt too long (max {_MAX_PROMPT_BYTES} bytes)",
                error_code="VISION_BAD_INPUT",
            )
        _model_arg = (args.get("model") or "").strip() or None
        if _model_arg:
            if _model_arg not in _ALLOWED_VISION_MODELS:
                return ToolResult.error_result(
                    f"model {_model_arg!r} not in allowed vision models; "
                    f"supported: {sorted(_ALLOWED_VISION_MODELS)}",
                    error_code="VISION_BAD_INPUT",
                )
            model = _model_arg
        else:
            model = _resolve_vision_model()

        sources = [s for s in (url, b64, path) if s]
        if not sources:
            return ToolResult.error_result(
                "must provide image_url, image_path, or image_base64",
                error_code="VISION_BAD_INPUT",
            )
        if len(sources) > 1:
            return ToolResult.error_result(
                "provide exactly ONE of image_url, image_path, image_base64 "
                "(got multiple)",
                error_code="VISION_BAD_INPUT",
            )
        if b64 and not mime:
            return ToolResult.error_result(
                "image_base64 requires mime_type",
                error_code="VISION_BAD_INPUT",
            )

        try:
            if b64:
                if len(b64) > _MAX_IMAGE_BYTES * 4 // 3 + 64:
                    return ToolResult.error_result(
                        f"base64 payload too large (exceeds {_MAX_IMAGE_BYTES} byte cap)",
                        error_code="VISION_IMAGE_TOO_LARGE",
                    )
                try:
                    image_bytes = base64.b64decode(b64, validate=True)
                except Exception as e:
                    return ToolResult.error_result(
                        f"invalid base64: {type(e).__name__}", error_code="VISION_BAD_INPUT", error=e
                    )
                if not mime.startswith(_ALLOWED_MIME_PREFIXES):
                    return ToolResult.error_result(
                        f"mime_type must start with image/, got {mime!r}",
                        error_code="VISION_BAD_INPUT",
                    )
                if mime not in _GEMINI_SUPPORTED_MIMES:
                    return ToolResult.error_result(
                        f"mime_type {mime!r} not supported by the vision model "
                        f"(supported: {sorted(_GEMINI_SUPPORTED_MIMES)})",
                        error_code="VISION_BAD_INPUT",
                    )
            elif path:
                image_bytes, mime = await _read_image_from_path(path, mime)
            else:
                image_bytes, mime = await _download_image(url)
        except VisionToolError as e:
            return ToolResult.error_result(str(e), error_code=e.code, error=e)
        except PathContainmentError as e:
            _try_audit_blocked(
                resource=path or url or "unknown",
                action="describe",
                details={"reason": "path_containment", "path": path},
            )
            return ToolResult.error_result(
                "image_path is outside allowed directories",
                error_code="VISION_BAD_INPUT",
                error=e,
            )
        except Exception as e:
            logger.exception("vision image acquisition failed")
            return ToolResult.error_result(
                f"image download failed: {type(e).__name__}",
                error_code="VISION_DOWNLOAD_FAILED",
                error=e,
            )

        if len(image_bytes) > _MAX_IMAGE_BYTES:
            return ToolResult.error_result(
                f"image exceeds {_MAX_IMAGE_BYTES} bytes ({len(image_bytes)} bytes)",
                error_code="VISION_IMAGE_TOO_LARGE",
            )

        # Pre-compute audit resource here so both the magic-byte block and the
        # success audit path use the same sanitised value.
        _audit_resource = _sanitise_url_for_audit(url) if url else (path or f"base64:{mime}")
        try:
            _validate_magic_bytes(image_bytes, mime)
        except VisionToolError as e:
            _try_audit_blocked(
                resource=_audit_resource,
                action="describe",
                details={"reason": "magic_byte_mismatch", "mime": mime},
                suspicious=True,
            )
            return ToolResult.error_result(str(e), error_code=e.code, error=e)

        sha8 = _sha8(image_bytes)
        try:
            description = await _gemini_describe(image_bytes, mime, prompt, model)
        except VisionToolError as e:
            return ToolResult.error_result(str(e), error_code=e.code, error=e)
        except asyncio.TimeoutError:
            # Python 3.11+: asyncio.TimeoutError is a CancelledError (BaseException)
            # so it escapes ``except Exception``; catch it explicitly here.
            return ToolResult.error_result(
                "vision LLM call failed: TimeoutError",
                error_code="VISION_LLM_FAILED",
            )
        except Exception as e:
            logger.exception("vision LLM call failed")
            return ToolResult.error_result(
                f"vision LLM call failed: {type(e).__name__}",
                error_code="VISION_LLM_FAILED",
                error=e,
            )

        result = ToolResult.success_result(
            data={
                "description": description,
                "model": model,
                "mime_type": mime,
                "size_bytes": len(image_bytes),
                "image_sha8": sha8,
            },
            message=f"vision_describe_image ok ({model}, {len(image_bytes)} B, sha8={sha8})",
        )
        try:
            from deile.security.audit_logger import (AuditEventType,
                                                     SeverityLevel,
                                                     get_audit_logger)
            _source = "url" if url else ("path" if path else "base64")
            get_audit_logger().log_event(
                event_type=AuditEventType.TOOL_EXECUTION,
                severity=SeverityLevel.INFO,
                actor="vision_describe_image",
                resource=_audit_resource,
                action="describe",
                result="success",
                details={"sha8": sha8, "mime_type": mime, "size_bytes": len(image_bytes), "model": model, "source": _source},
                tool_name="vision_describe_image",
            )
        except Exception:  # audit must never crash the tool
            logger.debug("audit emission failed", exc_info=True)
        return result


async def _read_image_from_path(path: str, mime_hint: str | None) -> tuple[bytes, str]:
    """Read image bytes from a local path in chunks (<=10 MiB cap).

    Chunked read avoids a TOCTOU window between ``os.path.getsize`` and the
    full ``read()`` -- a file that just-fits the size check could be swapped
    for a much larger one before the read completes. By streaming and
    counting bytes as they come, the cap is enforced atomically.

    MIME is auto-detected from extension when not provided; only formats
    Gemini accepts are auto-recognized (jpeg/png/webp/gif). Strips leading
    ``file://`` if present.
    """
    if path.startswith("file://"):
        _parsed = _urlparse(path)
        if _parsed.netloc and _parsed.netloc.lower() not in ("", "localhost"):
            _try_audit_blocked(
                resource=_sanitise_url_for_audit(path),
                action="describe",
                details={"reason": "file_uri_remote_authority", "authority": _parsed.hostname or ""},
                suspicious=True,
            )
            raise VisionToolError(
                "VISION_BAD_INPUT",
                f"file:// URI with non-localhost authority is not supported: {path!r}",
            )
        path = _urlunquote(_parsed.path)

    def _resolve_and_check(path_str: str) -> Path:
        resolved = Path(path_str).resolve()
        _assert_safe_root(resolved)
        return resolved

    p = await asyncio.to_thread(_resolve_and_check, path)
    if not await asyncio.to_thread(p.is_file):
        raise VisionToolError("VISION_BAD_INPUT", f"image_path not found: {path!r}")
    if not mime_hint:
        ext = p.suffix.lower()
        mime_hint = _EXT_TO_MIME.get(ext)
    if not mime_hint or not mime_hint.startswith(_ALLOWED_MIME_PREFIXES):
        raise VisionToolError(
            "VISION_BAD_INPUT",
            f"could not infer image MIME for {path!r}; pass mime_type explicitly",
        )
    if mime_hint not in _GEMINI_SUPPORTED_MIMES:
        raise VisionToolError(
            "VISION_BAD_INPUT",
            f"mime_type {mime_hint!r} not supported by the vision model "
            f"(supported: {sorted(_GEMINI_SUPPORTED_MIMES)})",
        )
    # TOCTOU residual risks (both accepted):
    # 1. Size-swap: aiofiles does not expose O_NOFOLLOW; chunked read enforces
    #    the size cap atomically so a race cannot smuggle a larger file past it.
    # 2. Symlink-swap: path.resolve() follows symlinks; a swap between resolve()
    #    and open() could redirect to a different (but still safe-root-contained)
    #    path. Cross-safe-root swaps are OS-level and outside Python's control.
    chunk = 64 * 1024
    buf = bytearray()
    try:
        async with aiofiles.open(p, "rb") as fh:
            while True:
                blob = await fh.read(chunk)
                if not blob:
                    break
                buf.extend(blob)
                if len(buf) > _MAX_IMAGE_BYTES:
                    raise VisionToolError(
                        "VISION_IMAGE_TOO_LARGE",
                        f"image_path exceeds {_MAX_IMAGE_BYTES} bytes",
                    )
    except VisionToolError:
        raise
    except OSError as e:
        raise VisionToolError("VISION_READ_FAILED", f"could not read {path!r}: {type(e).__name__}")
    return bytes(buf), mime_hint


def _try_audit_blocked(
    resource: str, action: str, details: dict, suspicious: bool = False
) -> None:
    """Emit a WARNING audit event for a security-relevant rejection (never raises).

    ``suspicious=True`` uses ``SUSPICIOUS_ACTIVITY`` (SSRF/network attacks);
    ``suspicious=False`` (default) uses ``PERMISSION_DENIED`` (path containment).
    """
    try:
        from deile.security.audit_logger import (AuditEventType, SeverityLevel,
                                                 get_audit_logger)
        event_type = (
            AuditEventType.SUSPICIOUS_ACTIVITY if suspicious
            else AuditEventType.PERMISSION_DENIED
        )
        get_audit_logger().log_event(
            event_type=event_type,
            severity=SeverityLevel.WARNING,
            actor="vision_describe_image",
            resource=resource,
            action=action,
            result="blocked",
            details=details,
            tool_name="vision_describe_image",
        )
    except Exception:
        logger.debug("blocked-audit emission failed", exc_info=True)


def _is_ssrf_blocked(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Return True if ``ip`` must be blocked to prevent SSRF.

    Uses explicit attribute checks in addition to ``is_global`` to cover ranges
    that Python < 3.11 incorrectly classified as global (e.g. CGN 100.64/10,
    documentation ranges 192.0.2/24, 198.51.100/24, 203.0.113/24).

    Additional IPv6 checks:
    - ``is_multicast``: Python 3.11 reports all ff00::/8 multicast as ``is_global=True``.
    - ``_SSRF_BLOCKED_IPV6_NETS``: covers ``fec0::/10`` deprecated site-local (RFC 3879),
      which Python <=3.13 also reports as ``is_global=True``.
    """
    if (
        not ip.is_global
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_unspecified
        or ip.is_multicast
    ):
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        # IPv4-mapped addresses (::ffff:x.x.x.x) carry IPv4 semantics; check the
        # embedded IPv4 address so ranges like ::ffff:169.254.x.x are not missed.
        if ip.ipv4_mapped is not None and _is_ssrf_blocked(ip.ipv4_mapped):
            return True
        return any(ip in net for net in _SSRF_BLOCKED_IPV6_NETS)
    return False


async def _check_ssrf(url: str) -> None:
    """Reject URLs that target private/loopback/reserved IP space (direct SSRF).

    Precondition: ``url`` must start with ``http://`` or ``https://`` (caller's
    responsibility — ``_download_image`` enforces this before calling here).

    ``follow_redirects=False`` in ``_download_image`` stops redirect-chain SSRF;
    this guard stops direct requests to RFC-1918, link-local, and loopback IPs.
    Monkeypatchable so tests that use 127.0.0.1 servers can bypass it.

    **Known limitation — DNS rebinding:** this function resolves the hostname
    via ``socket.getaddrinfo``, then ``httpx`` performs its own independent DNS
    lookup when opening the connection. An attacker controlling a domain with
    TTL=0 can return a public IP for our check and a private IP for httpx's
    connect, bypassing this guard. Full mitigation requires a custom
    ``httpx.AsyncHTTPTransport`` that validates the connected socket's peer IP;
    that is out of scope for this PR. Operators in high-threat environments
    should run DEILE behind an egress proxy that enforces IP-range policies.
    """
    _audit_url = _sanitise_url_for_audit(url)
    parsed = _urlparse(url)
    host = parsed.hostname or ""
    if not host:
        raise VisionToolError("VISION_BAD_INPUT", "cannot parse hostname from URL")
    # Fast path: bare IP literals can be checked without DNS
    try:
        ip = ipaddress.ip_address(host)
        if _is_ssrf_blocked(ip):
            _try_audit_blocked(
                resource=_audit_url, action="ssrf_check",
                details={"reason": "private_ip_literal", "ip": str(ip)},
                suspicious=True,
            )
            raise VisionToolError("VISION_BAD_INPUT", f"image_url targets non-public IP: {ip}")
        return
    except ValueError:
        pass
    # Hostname: resolve and validate every returned address
    try:
        infos = await asyncio.wait_for(
            asyncio.to_thread(socket.getaddrinfo, host, None, 0, socket.SOCK_STREAM),
            timeout=_DNS_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        raise VisionToolError("VISION_DOWNLOAD_FAILED", f"DNS resolution timed out for {host!r}")
    except OSError as exc:
        raise VisionToolError("VISION_DOWNLOAD_FAILED", f"DNS resolution failed for {host!r}: {type(exc).__name__}")
    for *_, sockaddr in infos:
        # Strip IPv6 zone ID (e.g. "fe80::1%eth0") before parsing; ipaddress
        # does not accept zone IDs and would raise ValueError, silently skipping
        # what is actually a link-local address.
        addr_str = sockaddr[0].split("%")[0]
        try:
            ip = ipaddress.ip_address(addr_str)
        except ValueError:
            raise VisionToolError(
                "VISION_BAD_INPUT",
                f"DNS resolved unparseable address for {host!r}",
            )
        if _is_ssrf_blocked(ip):
            _try_audit_blocked(
                resource=_audit_url, action="ssrf_check",
                details={"reason": "private_ip_resolved", "host": host, "ip": str(ip)},
                suspicious=True,
            )
            raise VisionToolError(
                "VISION_BAD_INPUT",
                f"image_url {host!r} resolves to non-public IP {ip}",
            )


def _validate_magic_bytes(image_bytes: bytes, mime: str) -> None:
    """Verify image bytes carry the expected magic signature for the declared MIME.

    Detects MIME spoofing: a server (or caller) claiming image/png while
    returning JavaScript/polyglot bytes.

    GIF: checks full 6-byte signature (GIF87a or GIF89a) to block GIF8<junk> polyglots.
    WebP: checks RIFF at [0:4] and WEBP at [8:12] (two-field format).
    JPEG/PNG: simple prefix match via _MAGIC_BYTES.
    """
    if mime == "image/gif":
        ok = image_bytes[:6] in (b"GIF87a", b"GIF89a")
    elif mime == "image/webp":
        ok = image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP"
    else:
        magic = _MAGIC_BYTES.get(mime)
        if not magic:
            logger.warning("no magic-byte entry for supported MIME %r; update _MAGIC_BYTES", mime)
            return
        ok = image_bytes[:len(magic)] == magic
    if not ok:
        raise VisionToolError("VISION_BAD_INPUT", f"image bytes do not match declared MIME {mime!r}")


async def _download_image(url: str) -> tuple[bytes, str]:
    """Fetch the image, capping size and time. Returns (bytes, mime_type).

    Plain HTTP is accepted (not HTTPS-only) to support internal/test servers.
    In production, Discord CDN URLs are always HTTPS; the SSRF guard in
    ``_check_ssrf`` provides the primary network-layer protection.
    """
    if not url.startswith(("http://", "https://")):
        raise VisionToolError("VISION_BAD_INPUT", "image_url must be http(s)")
    await _check_ssrf(url)
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=False) as client:
        async with client.stream("GET", url) as resp:
            if 300 <= resp.status_code < 400:
                _try_audit_blocked(
                    resource=_sanitise_url_for_audit(url),
                    action="download",
                    details={"reason": "redirect", "status": resp.status_code},
                    suspicious=True,
                )
                raise VisionToolError(
                    "VISION_DOWNLOAD_FAILED",
                    f"image_url redirects (status {resp.status_code}; follow_redirects disabled)",
                )
            if resp.status_code >= 400:
                raise VisionToolError(
                    "VISION_DOWNLOAD_FAILED",
                    f"upstream returned {resp.status_code}",
                )
            mime = (resp.headers.get("content-type") or "application/octet-stream").split(";")[0].strip()
            if not mime.startswith(_ALLOWED_MIME_PREFIXES):
                raise VisionToolError(
                    "VISION_BAD_INPUT",
                    f"upstream mime is not an image: {mime!r}",
                )
            if mime not in _GEMINI_SUPPORTED_MIMES:
                raise VisionToolError(
                    "VISION_BAD_INPUT",
                    f"upstream mime {mime!r} not supported by the vision model "
                    f"(supported: {sorted(_GEMINI_SUPPORTED_MIMES)})",
                )
            buf = bytearray()
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                buf.extend(chunk)
                if len(buf) > _MAX_IMAGE_BYTES:
                    raise VisionToolError(
                        "VISION_IMAGE_TOO_LARGE",
                        f"image exceeds {_MAX_IMAGE_BYTES} bytes",
                    )
            return bytes(buf), mime


async def _gemini_describe(
    image_bytes: bytes, mime: str, prompt: str, model: str
) -> str:
    """Call Gemini multimodal generate_content and return the text answer."""
    try:
        from google import genai
        from google.genai import types
    except ImportError as e:
        raise VisionToolError(
            "VISION_LLM_FAILED",
            "google-genai SDK not installed; install with `pip install google-genai`",
        ) from e

    from deile.config.settings import get_settings

    api_key = get_settings().api_keys.get("GOOGLE_API_KEY")
    if not api_key:
        raise VisionToolError(
            "VISION_LLM_FAILED",
            "GOOGLE_API_KEY not set; vision tool needs Gemini access",
        )

    def _call() -> Any:
        # If asyncio.wait_for cancels the outer coroutine, this thread continues
        # until _call returns; genai.Client.__exit__ fires on the thread but
        # socket cleanup is deferred to thread completion (threads cannot be cancelled).
        with genai.Client(api_key=api_key) as client:
            return client.models.generate_content(
                model=model,
                contents=[
                    types.Part.from_bytes(data=image_bytes, mime_type=mime),
                    prompt,
                ],
            )

    try:
        response = await asyncio.wait_for(asyncio.to_thread(_call), timeout=_GEMINI_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise VisionToolError("VISION_LLM_FAILED", "Gemini call timed out")
    text = getattr(response, "text", None)
    if not text:
        try:
            candidates = response.candidates
            for cand in candidates or []:
                content = getattr(cand, "content", None)
                if not content:
                    continue
                for part in getattr(content, "parts", None) or []:
                    pt = getattr(part, "text", None)
                    if pt:
                        return pt
        except Exception:
            logger.debug("Gemini candidate text extraction failed", exc_info=True)
        raise VisionToolError(
            "VISION_LLM_FAILED",
            "Gemini returned no text content",
        )
    return text
