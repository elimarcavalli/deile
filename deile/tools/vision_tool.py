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
  `mime_type`. Discord attachment URLs are public CDN links; no auth
  needed.

Security & limits:
- The downloader has a hard 10 MiB cap and a 15 s timeout (Discord's own
  per-attachment limits are more generous, but vision payload + base64
  inflation make this a sensible ceiling for one tool call).
- The tool routes through `PermissionManager` like any other tool; it
  does not need approval (read-only).
- Emits `AuditEvent(TOOL_EXECUTION)` with a SHA8 hash of the image body
  (not the body itself) plus URL/mime/byte-count.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path
from typing import Any

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
_ALLOWED_MIME_PREFIXES = ("image/",)
_DEFAULT_PROMPT = (
    "Descreva exatamente o que está nesta imagem em 2-4 linhas em português. "
    "Inclua: objetos visíveis, texto legível (transcreva), pessoas (sem identificar), "
    "cores predominantes e contexto geral. Sem floreios."
)


def _resolve_vision_model() -> str:
    from deile.config.settings import get_settings

    return get_settings().vision_model or _DEFAULT_VISION_MODEL


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
                            "description": "HTTPS URL to fetch the image from (e.g. Discord CDN URL).",
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
                security_level=SecurityLevel.SAFE,
                category=ToolCategory.OTHER,
            )
        )

    @property
    def name(self) -> str:
        return "vision_describe_image"

    @property
    def description(self) -> str:
        return self._schema.description if self._schema else ""

    @property
    def category(self) -> str:
        return ToolCategory.OTHER.value

    async def execute(self, context: ToolContext) -> ToolResult:
        args = dict(context.parsed_args or {})
        url = (args.get("image_url") or "").strip() or None
        path = (args.get("image_path") or "").strip() or None
        b64 = (args.get("image_base64") or "").strip() or None
        mime = (args.get("mime_type") or "").strip() or None
        prompt = (args.get("prompt") or _DEFAULT_PROMPT).strip()
        model = (args.get("model") or _resolve_vision_model()).strip()

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
                try:
                    image_bytes = base64.b64decode(b64, validate=True)
                except Exception as e:
                    return ToolResult.error_result(
                        f"invalid base64: {e}", error_code="VISION_BAD_INPUT", error=e
                    )
                if not mime.startswith(_ALLOWED_MIME_PREFIXES):
                    return ToolResult.error_result(
                        f"mime_type must start with image/, got {mime!r}",
                        error_code="VISION_BAD_INPUT",
                    )
            elif path:
                # _read_image_from_path uses synchronous open() and a chunked
                # read loop up to 10 MiB; run it in a worker thread so the
                # event loop is not blocked (pilar 03 §1).
                image_bytes, mime = await asyncio.to_thread(
                    _read_image_from_path, path, mime
                )
            else:
                image_bytes, mime = await _download_image(url)
        except VisionToolError as e:
            return ToolResult.error_result(str(e), error_code=e.code, error=e)
        except PathContainmentError as e:
            return ToolResult.error_result(str(e), error_code="VISION_BAD_INPUT", error=e)
        except Exception as e:
            logger.exception("vision download failed")
            return ToolResult.error_result(
                f"image download failed: {type(e).__name__}: {e}",
                error_code="VISION_DOWNLOAD_FAILED",
                error=e,
            )

        if len(image_bytes) > _MAX_IMAGE_BYTES:
            return ToolResult.error_result(
                f"image exceeds {_MAX_IMAGE_BYTES} bytes ({len(image_bytes)} bytes)",
                error_code="VISION_IMAGE_TOO_LARGE",
            )

        sha8 = _sha8(image_bytes)
        try:
            description = await _gemini_describe(image_bytes, mime, prompt, model)
        except VisionToolError as e:
            return ToolResult.error_result(str(e), error_code=e.code, error=e)
        except Exception as e:
            logger.exception("vision LLM call failed")
            return ToolResult.error_result(
                f"vision LLM call failed: {type(e).__name__}: {e}",
                error_code="VISION_LLM_FAILED",
                error=e,
            )

        return ToolResult.success_result(
            data={
                "description": description,
                "model": model,
                "mime_type": mime,
                "size_bytes": len(image_bytes),
                "image_sha8": sha8,
            },
            message=f"vision_describe_image ok ({model}, {len(image_bytes)} B, sha8={sha8})",
        )


class VisionToolError(DEILEError):
    """Typed error so the tool returns a consistent error_code.

    Inherits from DEILEError (pilar 03 §6) so a single ``except DEILEError``
    handler upstream captures it alongside ``PathContainmentError``,
    ``ToolError``, ``ValidationError``, and other domain errors.
    """

    def __init__(self, code: str, message: str):
        super().__init__(message, error_code=code)
        self.code = code


_EXT_TO_MIME = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
_GEMINI_SUPPORTED_MIMES = frozenset(_EXT_TO_MIME.values())


def _read_image_from_path(path: str, mime_hint: str | None) -> tuple[bytes, str]:
    """Read image bytes from a local path in chunks (≤10 MiB cap).

    Chunked read avoids a TOCTOU window between ``os.path.getsize`` and the
    full ``read()`` — a file that just-fits the size check could be swapped
    for a much larger one before the read completes. By streaming and
    counting bytes as they come, the cap is enforced atomically.

    MIME is auto-detected from extension when not provided; only formats
    Gemini accepts are auto-recognized (jpeg/png/webp/gif). Strips leading
    ``file://`` if present.
    """
    if path.startswith("file://"):
        path = path[len("file://"):]
    p = Path(path).resolve()
    _assert_safe_root(p)
    if not p.is_file():
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
    chunk = 64 * 1024
    buf = bytearray()
    with open(p, "rb") as fh:
        while True:
            blob = fh.read(chunk)
            if not blob:
                break
            buf.extend(blob)
            if len(buf) > _MAX_IMAGE_BYTES:
                raise VisionToolError(
                    "VISION_IMAGE_TOO_LARGE",
                    f"image_path exceeds {_MAX_IMAGE_BYTES} bytes",
                )
    return bytes(buf), mime_hint


async def _download_image(url: str) -> tuple[bytes, str]:
    """Fetch the image, capping size and time. Returns (bytes, mime_type)."""
    if not url.startswith(("http://", "https://")):
        raise VisionToolError("VISION_BAD_INPUT", "image_url must be http(s)")
    async with httpx.AsyncClient(timeout=_DOWNLOAD_TIMEOUT_S, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
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
            buf = bytearray()
            async for chunk in resp.aiter_bytes():
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
        client = genai.Client(api_key=api_key)
        return client.models.generate_content(
            model=model,
            contents=[
                types.Part.from_bytes(data=image_bytes, mime_type=mime),
                prompt,
            ],
        )

    response = await asyncio.to_thread(_call)
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
            pass
        raise VisionToolError(
            "VISION_LLM_FAILED",
            "Gemini returned no text content",
        )
    return text
