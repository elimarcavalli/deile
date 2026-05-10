"""Unit tests for VisionDescribeImageTool.

All tests are hermetic:
- No real Gemini calls — ``_gemini_describe`` is monkeypatched.
- URL download uses a real httpx mock server (``image_server`` fixture) so
  the streaming / size-cap logic runs for real.
"""
from __future__ import annotations

import pytest

from deile.tools.base import ToolContext, ToolStatus
from deile.tools.vision_tool import VisionDescribeImageTool

# ---------------------------------------------------------------------------
# Minimal valid 1×1 PNG (67 bytes)
# ---------------------------------------------------------------------------
PNG_1x1_BYTES = (
    b"\x89PNG\r\n\x1a\n"
    b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02"
    b"\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
    b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
)


@pytest.fixture
def tool():
    return VisionDescribeImageTool()


@pytest.fixture
def ctx_factory():
    def _make(**kwargs):
        return ToolContext(user_input="", parsed_args=kwargs)
    return _make


# ---------------------------------------------------------------------------
# Input validation (no I/O)
# ---------------------------------------------------------------------------


async def test_no_source_returns_error(tool, ctx_factory):
    res = await tool.execute(ctx_factory())
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_url_and_base64_together_returns_error(tool, ctx_factory):
    res = await tool.execute(ctx_factory(
        image_url="https://example.com/img.png",
        image_base64="abc",
    ))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_base64_without_mime_returns_error(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_base64="abc"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_invalid_base64_returns_error(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_base64="!!!", mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_non_image_mime_returns_error(tool, ctx_factory):
    import base64
    b64 = base64.b64encode(b"data").decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="text/plain"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_url_only_accepts_http(tool, ctx_factory):
    res = await tool.execute(ctx_factory(image_url="ftp://bad.example.com/img.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_all_three_sources_returns_error(tool, ctx_factory):
    """Multiple input sources at once → VISION_BAD_INPUT (no silent pick)."""
    import base64
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(
        image_url="https://example.com/img.png",
        image_base64=b64,
        image_path="/some/path.png",
        mime_type="image/png",
    ))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---------------------------------------------------------------------------
# base64 happy path
# ---------------------------------------------------------------------------


async def test_base64_happy_path(tool, ctx_factory, monkeypatch):
    import base64

    async def fake(image_bytes, mime, prompt, model):
        return "a cat"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_success
    assert res.data["description"] == "a cat"
    assert res.data["mime_type"] == "image/png"
    assert len(res.data["image_sha8"]) == 8


# ---------------------------------------------------------------------------
# URL download
# ---------------------------------------------------------------------------


@pytest.fixture
def image_server(httpserver):
    """Serve a minimal PNG over HTTP for download tests."""
    httpserver.expect_request("/img.png").respond_with_data(
        PNG_1x1_BYTES, content_type="image/png"
    )
    httpserver.expect_request("/big.png").respond_with_data(
        b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024),
        content_type="image/png",
    )
    httpserver.expect_request("/notfound.png").respond_with_data(
        "", status=404
    )
    httpserver.expect_request("/text.txt").respond_with_data(
        "hello", content_type="text/plain"
    )
    return httpserver


async def test_url_download_happy(tool, ctx_factory, monkeypatch, image_server):
    async def fake(image_bytes, mime, prompt, model):
        return "a tiny PNG"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    url = image_server.url_for("/img.png")
    res = await tool.execute(ctx_factory(image_url=url))
    assert res.is_success
    assert res.data["description"] == "a tiny PNG"


async def test_url_download_too_large(tool, ctx_factory, image_server):
    url = image_server.url_for("/big.png")
    res = await tool.execute(ctx_factory(image_url=url))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_IMAGE_TOO_LARGE"


async def test_url_download_404(tool, ctx_factory, image_server):
    url = image_server.url_for("/notfound.png")
    res = await tool.execute(ctx_factory(image_url=url))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_DOWNLOAD_FAILED"


async def test_url_download_not_image(tool, ctx_factory, image_server):
    url = image_server.url_for("/text.txt")
    res = await tool.execute(ctx_factory(image_url=url))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---- image_path -------------------------------------------------------------


async def test_image_path_happy(tool, ctx_factory, monkeypatch, repo_tmp_path):
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured.update(bytes_len=len(image_bytes), mime=mime)
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    p = repo_tmp_path / "img.png"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_success, res.message
    assert captured["mime"] == "image/png"
    assert captured["bytes_len"] == len(PNG_1x1_BYTES)


async def test_image_path_with_file_scheme(tool, ctx_factory, monkeypatch, repo_tmp_path):
    async def fake(image_bytes, mime, prompt, model):
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    p = repo_tmp_path / "x.jpeg"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=f"file://{p}"))
    assert res.is_success
    assert res.data["mime_type"] == "image/jpeg"


async def test_image_path_missing(tool, ctx_factory):
    # A path outside safe roots is rejected with VISION_BAD_INPUT (path
    # containment check fires before the filesystem stat).
    res = await tool.execute(ctx_factory(image_path="/no/such/file.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_unknown_extension_requires_mime(tool, ctx_factory, repo_tmp_path):
    p = repo_tmp_path / "noext"
    p.write_bytes(PNG_1x1_BYTES)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_bmp_rejected_before_llm(tool, ctx_factory, repo_tmp_path):
    """Gemini doesn't support image/bmp. Reject before the LLM call so the
    user sees a clear error instead of a confusing VISION_LLM_FAILED."""
    p = repo_tmp_path / "x.bmp"
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
    tool, ctx_factory, monkeypatch, repo_tmp_path
):
    """Cap is enforced *during* the read, not against pre-measured size,
    so a TOCTOU swap can't bypass it. Simulated by writing a file larger
    than the cap and asserting the failure is VISION_IMAGE_TOO_LARGE."""
    from deile.tools.vision_tool import _MAX_IMAGE_BYTES
    p = repo_tmp_path / "huge.png"
    # 1 MiB beyond the cap
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * (_MAX_IMAGE_BYTES + 1024))
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_IMAGE_TOO_LARGE"


# ---- llm error mapping ------------------------------------------------------


async def test_llm_failure_returns_typed_error(tool, ctx_factory, monkeypatch):
    import base64

    async def fail(image_bytes, mime, prompt, model):
        from deile.tools.vision_tool import VisionToolError
        raise VisionToolError("VISION_LLM_FAILED", "quota exceeded")

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fail)
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_LLM_FAILED"
    assert "quota" in res.message


# ---- registry ---------------------------------------------------------------


def test_registered_by_auto_discover():
    from deile.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.auto_discover()
    tool = reg.get("vision_describe_image")
    assert tool is not None
