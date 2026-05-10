"""Unit tests for VisionDescribeImageTool.

All tests are hermetic:
- No real Gemini calls -- ``_gemini_describe`` is monkeypatched.
- URL download uses a real aiohttp server (``image_server`` fixture) so
  the streaming / size-cap logic runs for real.
"""
from __future__ import annotations

import pytest

from deile.tools.base import ToolContext
from deile.tools.vision_tool import VisionDescribeImageTool

# ---------------------------------------------------------------------------
# Minimal valid 1x1 PNG (67 bytes)
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
    """Multiple input sources at once -> VISION_BAD_INPUT (no silent pick)."""
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


async def _no_ssrf_check(url: str) -> None:
    """SSRF no-op for tests that use 127.0.0.1 servers (not testing SSRF behaviour)."""


@pytest.fixture
async def image_server(monkeypatch):
    """Self-contained aiohttp server for URL download tests (no extra packages needed).

    Monkeypatches _check_ssrf to a no-op because the server binds to 127.0.0.1
    (loopback), which would otherwise be rejected as a non-public IP.
    Tests that specifically exercise SSRF behaviour should set up their own
    server and NOT use this fixture.
    """
    monkeypatch.setattr("deile.tools.vision_tool._check_ssrf", _no_ssrf_check)
    from aiohttp import web

    big_body = b"\x89PNG\r\n\x1a\n" + b"\x00" * (11 * 1024 * 1024)

    routes = web.RouteTableDef()

    @routes.get("/img.png")
    async def _img(_):
        return web.Response(body=PNG_1x1_BYTES, content_type="image/png")

    @routes.get("/big.png")
    async def _big(_):
        return web.Response(body=big_body, content_type="image/png")

    @routes.get("/notfound.png")
    async def _404(_):
        return web.Response(status=404)

    @routes.get("/text.txt")
    async def _txt(_):
        return web.Response(body=b"hello", content_type="text/plain")

    app = web.Application()
    app.add_routes(routes)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    class _Server:
        def url_for(self, path: str) -> str:
            return f"{base}{path}"

    yield _Server()
    await runner.cleanup()


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
    # Use valid JPEG magic bytes so the magic-byte validation passes.
    p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 16)
    res = await tool.execute(ctx_factory(image_path=f"file://{p}"))
    assert res.is_success
    assert res.data["mime_type"] == "image/jpeg"


async def test_image_path_outside_safe_root_rejected(tool, ctx_factory):
    # A path outside safe roots is rejected with VISION_BAD_INPUT; the
    # containment check fires before the filesystem stat.
    res = await tool.execute(ctx_factory(image_path="/no/such/file.png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_image_path_not_found(tool, ctx_factory, repo_tmp_path):
    """A path inside safe roots that does not exist returns VISION_BAD_INPUT."""
    res = await tool.execute(ctx_factory(image_path=str(repo_tmp_path / "nonexistent.png")))
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


async def test_llm_generic_exception_returns_vision_llm_failed(tool, ctx_factory, monkeypatch):
    """A non-VisionToolError from _gemini_describe must return VISION_LLM_FAILED."""
    async def fail(image_bytes, mime, prompt, model):
        raise RuntimeError("unexpected SDK error")

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fail)
    import base64
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_LLM_FAILED"


# ---- registry ---------------------------------------------------------------


def test_registered_by_auto_discover():
    from deile.tools.registry import ToolRegistry

    reg = ToolRegistry()
    reg.auto_discover()
    tool = reg.get("vision_describe_image")
    assert tool is not None


# ---- model allowlist --------------------------------------------------------


async def test_unknown_model_returns_bad_input(tool, ctx_factory):
    """Caller-supplied model not in allowlist must be rejected."""
    import base64
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(
        image_base64=b64, mime_type="image/png", model="gemini-ultra"
    ))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_allowed_model_passes(tool, ctx_factory, monkeypatch):
    """A model in the allowlist must not be rejected at the gate."""
    async def fake(image_bytes, mime, prompt, model):
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    import base64
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(
        image_base64=b64, mime_type="image/png", model="gemini-2.5-flash"
    ))
    assert res.is_success


# ---- base64 size pre-check --------------------------------------------------


async def test_base64_oversized_rejected_before_decode(tool, ctx_factory):
    """Oversized base64 string must be rejected before b64decode allocation."""
    from deile.tools.vision_tool import _MAX_IMAGE_BYTES
    oversized_b64 = "A" * (_MAX_IMAGE_BYTES * 2)
    res = await tool.execute(ctx_factory(image_base64=oversized_b64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_IMAGE_TOO_LARGE"


# ---- MIME allowlist consistency (b64 and URL paths) -------------------------


async def test_b64_unsupported_mime_rejected(tool, ctx_factory):
    """image/svg+xml is a valid image/ prefix but not in _GEMINI_SUPPORTED_MIMES."""
    import base64
    b64 = base64.b64encode(b"<svg/>").decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/svg+xml"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_url_unsupported_mime_rejected(tool, ctx_factory, monkeypatch):
    """URL returning image/tiff (valid prefix, unsupported by Gemini) must be rejected."""
    # Bypass SSRF check: test is about MIME rejection, not SSRF.
    monkeypatch.setattr("deile.tools.vision_tool._check_ssrf", _no_ssrf_check)
    from aiohttp import web

    async def _tiff(req):
        return web.Response(body=PNG_1x1_BYTES, content_type="image/tiff")

    app = web.Application()
    app.router.add_get("/tiff.tiff", _tiff)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/tiff.tiff"
    try:
        res = await tool.execute(ctx_factory(image_url=url))
    finally:
        await runner.cleanup()
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---- file:// authority rejection --------------------------------------------


async def test_file_uri_with_non_localhost_authority_rejected(tool, ctx_factory):
    """file://hostname/path must be rejected -- non-localhost authority is unsafe."""
    res = await tool.execute(ctx_factory(image_path="file://remotehost/etc/passwd"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---- prompt length cap ------------------------------------------------------


async def test_prompt_too_long_returns_bad_input(tool, ctx_factory):
    """A prompt longer than _MAX_PROMPT_BYTES must be rejected."""
    import base64

    from deile.tools.vision_tool import _MAX_PROMPT_BYTES
    b64 = base64.b64encode(b"x").decode()
    long_prompt = "a" * (_MAX_PROMPT_BYTES + 1)
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png", prompt=long_prompt))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


# ---- SSRF: redirects not followed -------------------------------------------


async def test_url_redirect_not_followed(tool, ctx_factory, monkeypatch):
    """HTTP redirects must not be followed (prevents SSRF via redirect chains)."""
    # Bypass IP-range SSRF check: test is about redirect handling, not direct SSRF.
    monkeypatch.setattr("deile.tools.vision_tool._check_ssrf", _no_ssrf_check)
    from aiohttp import web

    async def _redirect(req):
        raise web.HTTPFound(location="http://169.254.169.254/latest/meta-data/")

    app = web.Application()
    app.router.add_get("/redirect", _redirect)
    runner = web.AppRunner(app, handle_signals=False, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host="127.0.0.1", port=0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    url = f"http://127.0.0.1:{port}/redirect"
    try:
        res = await tool.execute(ctx_factory(image_url=url))
    finally:
        await runner.cleanup()
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_DOWNLOAD_FAILED"


# ---- OSError during file read -----------------------------------------------


async def test_image_path_read_error_returns_vision_read_failed(
    tool, ctx_factory, monkeypatch, repo_tmp_path
):
    """A PermissionError / OSError during read must return VISION_READ_FAILED."""

    class _PermErrorCtx:
        async def __aenter__(self):
            raise PermissionError("[Errno 13] Permission denied")

        async def __aexit__(self, *args):
            pass

    def _failing_open(path, mode="r", **kwargs):
        return _PermErrorCtx()

    monkeypatch.setattr("deile.tools.vision_tool.aiofiles.open", _failing_open)
    p = repo_tmp_path / "img.png"
    p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 10)
    res = await tool.execute(ctx_factory(image_path=str(p)))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_READ_FAILED"


# ---- audit blocked on path containment --------------------------------------


async def test_path_containment_emits_blocked_audit(tool, ctx_factory, monkeypatch):
    """_try_audit_blocked must be called when a PathContainmentError is raised."""
    calls = []

    def _fake_audit(resource, action, details, suspicious=False):
        calls.append({"resource": resource, "action": action, "details": details, "suspicious": suspicious})

    monkeypatch.setattr("deile.tools.vision_tool._try_audit_blocked", _fake_audit)
    res = await tool.execute(ctx_factory(image_path="/etc/passwd"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"
    assert calls, "_try_audit_blocked was not called on path containment violation"
    assert calls[0]["suspicious"] is False


# ---- direct private-IP SSRF rejection ---------------------------------------


async def test_direct_private_ip_rejected(tool, ctx_factory):
    """A URL with a private/loopback IP must be rejected without connecting."""
    for bad_url in (
        "http://169.254.169.254/latest/meta-data/",
        "http://10.0.0.1/admin",
        "http://192.168.1.1/secret",
    ):
        res = await tool.execute(ctx_factory(image_url=bad_url))
        assert res.is_error, f"expected error for {bad_url}"
        assert res.metadata["error_code"] == "VISION_BAD_INPUT", bad_url


async def test_direct_ipv6_private_rejected(tool, ctx_factory):
    """IPv6 loopback, link-local, multicast, and deprecated site-local must all be blocked."""
    for bad_url in (
        "http://[::1]/",        # IPv6 loopback
        "http://[fe80::1]/",    # IPv6 link-local
        "http://[ff02::1]/",    # IPv6 multicast (is_global=True on Python 3.11)
        "http://[fec0::1]/",    # IPv6 deprecated site-local RFC 3879 (is_global=True on Python 3.11)
    ):
        res = await tool.execute(ctx_factory(image_url=bad_url))
        assert res.is_error, f"expected error for {bad_url}"
        assert res.metadata["error_code"] == "VISION_BAD_INPUT", bad_url


async def test_direct_ip_ssrf_emits_suspicious_audit(tool, ctx_factory, monkeypatch):
    """SSRF rejection of a private IP literal must emit _try_audit_blocked(suspicious=True)."""
    calls = []

    def _fake_audit(resource, action, details, suspicious=False):
        calls.append({"suspicious": suspicious, "action": action})

    monkeypatch.setattr("deile.tools.vision_tool._try_audit_blocked", _fake_audit)
    res = await tool.execute(ctx_factory(image_url="http://169.254.169.254/"))
    assert res.is_error
    assert calls, "_try_audit_blocked was not called on SSRF rejection"
    assert calls[0]["suspicious"] is True


# ---- magic-byte validation --------------------------------------------------


async def test_magic_byte_mismatch_rejected(tool, ctx_factory):
    """Bytes that don't match the declared MIME must be rejected before the LLM call."""
    import base64

    # Pass JPEG magic bytes with mime=image/png -> mismatch; _gemini_describe is never reached
    jpeg_magic = b"\xff\xd8\xff" + b"\x00" * 10
    b64 = base64.b64encode(jpeg_magic).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_webp_valid_magic_passes(tool, ctx_factory, monkeypatch):
    """RIFF????WEBP (WebP compound magic) must pass validation."""
    async def fake(image_bytes, mime, prompt, model):
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    import base64
    # Minimal WebP: RIFF + 4-byte size + WEBP fourcc
    webp_bytes = b"RIFF" + b"\x00" * 4 + b"WEBP" + b"\x00" * 10
    b64 = base64.b64encode(webp_bytes).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/webp"))
    assert res.is_success


async def test_webp_riff_without_webp_fourcc_rejected(tool, ctx_factory):
    """RIFF container with non-WEBP fourcc (e.g. AVI) must fail magic-byte check."""
    import base64
    riff_avi = b"RIFF" + b"\x00" * 4 + b"AVI " + b"\x00" * 10
    b64 = base64.b64encode(riff_avi).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/webp"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"


async def test_magic_byte_mismatch_emits_blocked_audit(tool, ctx_factory, monkeypatch):
    """_try_audit_blocked(suspicious=True) must be called on MIME-spoofing detection."""
    calls = []

    def _fake_audit(resource, action, details, suspicious=False):
        calls.append({"resource": resource, "action": action, "details": details, "suspicious": suspicious})

    monkeypatch.setattr("deile.tools.vision_tool._try_audit_blocked", _fake_audit)
    import base64
    jpeg_magic = b"\xff\xd8\xff" + b"\x00" * 10
    b64 = base64.b64encode(jpeg_magic).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_error
    assert res.metadata["error_code"] == "VISION_BAD_INPUT"
    assert calls, "_try_audit_blocked was not called on magic-byte mismatch"
    assert calls[0]["suspicious"] is True


# ---- operator model env-var allowlist enforcement ---------------------------


async def test_operator_model_env_var_invalid_falls_back_to_default(
    tool, ctx_factory, monkeypatch
):
    """An invalid DEILE_VISION_MODEL env-var must fall back to the default model."""
    captured = {}

    async def fake(image_bytes, mime, prompt, model):
        captured["model"] = model
        return "ok"

    monkeypatch.setattr("deile.tools.vision_tool._gemini_describe", fake)
    # Simulate settings returning an invalid model name
    import deile.tools.vision_tool as vt
    monkeypatch.setattr(vt, "_resolve_vision_model", lambda: vt._DEFAULT_VISION_MODEL)
    import base64
    b64 = base64.b64encode(PNG_1x1_BYTES).decode()
    res = await tool.execute(ctx_factory(image_base64=b64, mime_type="image/png"))
    assert res.is_success
    assert captured["model"] == vt._DEFAULT_VISION_MODEL
