"""Tests for the vision_describe_image tool.

We don't hit the real Gemini API in unit tests — we monkeypatch
`_gemini_describe` to return a canned answer and assert the contract:
input validation, downloader limits, error code mapping, audit-friendly
return shape.

The actual Gemini call is exercised once in
`deile/tests/might/test_vision_live.py` (gated by env, costs real money).
"""

from __future__ import annotations

import base64
from typing import Tuple

import pytest

from deile.tools.base import ToolContext
from deile.tools.vision_tool import (_MAX_IMAGE_BYTES, VisionDescribeImageTool,
                                     VisionToolError)

# 1x1 white PNG
PNG_1x1_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8/5+hHgAHggJ/PchI7wAAAABJRU5ErkJggg=="
)
PNG_1x1_B64 = base64.b64encode(PNG_1x1_BYTES).decode("ascii")


@pytest.fixture
def tool():
    return VisionDescribeImageTool()


@pytest.fixture
def ctx_factory():
    def make(**args):
        return ToolContext(user_input="", parsed_args=args, session_data={})

    return make


# ---- input validation -------------------------------------------------------


async def test_requires_either_url_or_base64(tool, ctx_factory):
    res = await tool.execute(ctx_factory())
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_base64_requires_mime(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_base64=PNG_1x1_B64))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_invalid_base64(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_base64="not-base64!!", mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_non_image_mime_rejected(tool, ctx_factory):
    res = await tool.execute(
        ctx_factory(image_base64=PNG_1x1_B64, mime_type="text/plain")
    )
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_url_must_be_http(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_url="ftp://example.com/x.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_ambiguous_input_rejected(tool, ctx_factory):
    """Multiple input sources at once → VISION_BAD_INPUT (no silent pick)."""
    res = await tool.execute(
        ctx_factory(
            image_base64=PNG_1x1_B64,
            mime_type="image/png",
            image_url="https://example.com/x.png",
        )
    )
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"
    assert "exactly ONE" in res.message


# ---- happy path with monkeypatched gemini -----------------------------------


async def test_base64_happy_path(tool, ctx_factory, monkeypatch):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured.update(bytes_len=len(image_bytes), mime=mime, prompt=prompt, model=model)
        return "uma imagem branca de 1x1 pixel"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)

    res = await tool.execute(
        ctx_factory(
            image_base64=PNG_1x1_B64, mime_type="image/png", prompt="describe it"
        )
    )
    assert res.is_success, res.message
    assert res.data["description"] == "uma imagem branca de 1x1 pixel"
    assert res.data["mime_type"] == "image/png"
    assert res.data["size_bytes"] == len(PNG_1x1_BYTES)
    assert len(res.data["image_sha8"]) == 8
    assert captured["bytes_len"] == len(PNG_1x1_BYTES)
    assert captured["prompt"] == "describe it"


async def test_default_model_is_flash_lite(tool, ctx_factory, monkeypatch):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured["model"] = model
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    monkeypatch.delenv("DEILE_VISION_MODEL", raising=False)

    await tool.execute(ctx_factory(image_base64=PNG_1x1_B64, mime_type="image/png"))
    assert captured["model"] == "gemini-2.5-flash-lite"


async def test_env_override_picks_model(tool, ctx_factory, monkeypatch):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured["model"] = model
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    monkeypatch.setenv("DEILE_VISION_MODEL", "gemini-3-flash-preview")

    await tool.execute(ctx_factory(image_base64=PNG_1x1_B64, mime_type="image/png"))
    assert captured["model"] == "gemini-3-flash-preview"


async def test_explicit_model_arg_wins(tool, ctx_factory, monkeypatch):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured["model"] = model
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    monkeypatch.setenv("DEILE_VISION_MODEL", "gemini-3-flash-preview")

    await tool.execute(
        ctx_factory(
            image_base64=PNG_1x1_B64, mime_type="image/png", model="custom-vision"
        )
    )
    assert captured["model"] == "custom-vision"


# ---- url path with stub server ----------------------------------------------


@pytest.fixture
async def image_server() -> Tuple[str, "TestServer"]:  # noqa: F821
    from aiohttp import web

    routes = web.RouteTableDef()

    @routes.get("/image.png")
    async def _img(_):
        return web.Response(body=PNG_1x1_BYTES, content_type="image/png")

    @routes.get("/notfound.png")
    async def _404(_):
        return web.Response(status=404)

    @routes.get("/notimage.png")
    async def _txt(_):
        return web.Response(body=b"hello", content_type="text/plain")

    @routes.get("/huge.png")
    async def _huge(_):
        big = b"\x00" * (_MAX_IMAGE_BYTES + 100)
        return web.Response(body=big, content_type="image/png")

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    yield f"http://127.0.0.1:{port}", site
    await runner.cleanup()


async def test_url_download_happy(tool, ctx_factory, monkeypatch, image_server):
    base, _ = image_server

    async def fake(image_bytes, mime, prompt, model):
        return f"got {len(image_bytes)} bytes mime={mime}"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)

    res = await tool.execute(ctx_factory(image_url=f"{base}/image.png"))
    assert res.is_success, res.message
    assert "image/png" in res.data["description"]


async def test_url_download_404(tool, ctx_factory, monkeypatch, image_server):
    base, _ = image_server
    res = await tool.execute(ctx_factory(image_url=f"{base}/notfound.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_DOWNLOAD_FAILED"


async def test_url_download_not_image(tool, ctx_factory, image_server):
    base, _ = image_server
    res = await tool.execute(ctx_factory(image_url=f"{base}/notimage.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_url_download_too_large(tool, ctx_factory, image_server):
    base, _ = image_server
    res = await tool.execute(ctx_factory(image_url=f"{base}/huge.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_IMAGE_TOO_LARGE"


# ---- image_path -------------------------------------------------------------


async def test_image_path_happy(tool, ctx_factory, monkeypatch, tmp_path):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured.update(bytes_len=len(image_bytes), mime=mime)
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    p = tmp_path / "img.png"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_success, res.message
    assert captured["mime"] == "image/png"
    assert captured["bytes_len"] == len(PNG_1x1_BYTES)


async def test_image_path_with_file_scheme(tool, ctx_factory, monkeypatch, tmp_path):
    async def fake(image_bytes, mime, prompt, model):
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    p = tmp_path / "x.jpeg"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=f"file://{p}"))
    assert res.is_success
    assert res.data["mime_type"] == "image/jpeg"


async def test_image_path_missing(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_path="/no/such/file.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_unknown_extension_requires_mime(tool, ctx_factory, tmp_path):
    p = tmp_path / "noext"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_bmp_rejected_before_llm(tool, ctx_factory, tmp_path):
    """Gemini doesn't support image/bmp. Reject before the LLM call so the
    user sees a clear error instead of a confusing VISION_LLM_FAILED."""
    p = tmp_path / "x.bmp"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"
    # Even when MIME passed explicitly, BMP must be refused early.
    res2 = await tool.execute(
        ctx_factory(image_path=str(p), mime_type="image/bmp")
    )
    assert res2.is_error
    assert res2.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_chunked_read_caps_oversize_atomic(
    tool, ctx_factory, monkeypatch, tmp_path
):
    """Cap is enforced *during* the read, not against pre-measured size,
    so a TOCTOU swap can't bypass it. Simulated by writing a file larger
    than the cap and asserting the failure is VISION_IMAGE_TOO_LARGE."""
    from deile.tools.vision_tool import _MAX_IMAGE_BYTES
    p = tmp_path / "huge.png"
    # 1 MiB beyond the cap
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (_MAX_IMAGE_BYTES + 1024))
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_IMAGE_TOO_LARGE"


# ---- llm error mapping ------------------------------------------------------


async def test_llm_failure_returns_typed_error(tool, ctx_factory, monkeypatch):
    async def fake(image_bytes, mime, prompt, model):
        raise VisionToolError("VISION_LLM_FAILED", "out of quota")

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    res = await tool.execute(ctx_factory(image_base64=PNG_1x1_B64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_LLM_FAILED"


# ---- schema sanity ----------------------------------------------------------


def test_schema_serializes(tool):
    s = tool.schema.to_anthropic_tool()
    assert s["name"] == "vision_describe_image"
    assert s["input_schema"]["type"] == "object"

    s2 = tool.schema.to_openai_function()
    assert s2["function"]["name"] == "vision_describe_image"

    s3 = tool.schema.to_gemini_function()
    assert getattr(s3, "name", None) == "vision_describe_image"


def test_registered_by_auto_discover():
    from deile.tools.registry import ToolRegistry

    reg = ToolRegistry()
    n = reg.auto_discover()
    assert n >= 1
    assert reg.get("vision_describe_image") is not None
