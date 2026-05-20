"""Unit tests for ``deile/tools/dispatch_deile_task``.

Pin the security validators introduced in commit 808091e:

* ``_validate_bearer_token`` rejects control chars (CR/LF/NUL/TAB/space)
  and accepts RFC 6750 ``token68`` ASCII values.
* ``_worker_endpoint`` rejects non-HTTP schemes (``file://``, ``gopher://``,
  empty scheme) and URLs without a host.
* ``DispatchDeileTaskTool.execute`` rejects unknown personas with
  ``BAD_REQUEST`` (defense-in-depth, independent of JSON-Schema enum).

All tests are fully offline — ``_post_dispatch`` is monkeypatched so no
real HTTP traffic happens. The dispatch class-level cooldown registry
is cleared between tests so they don't leak state.
"""
from __future__ import annotations

import pytest

from deile.tools import dispatch_deile_task as dd
from deile.tools.base import ToolContext


@pytest.fixture(autouse=True)
def _clear_dispatch_cooldown():
    """Wipe the class-level cooldown cache between tests.

    ``DispatchDeileTaskTool._LAST_DISPATCH`` is a class-level dict, so a
    successful dispatch in one test would block any subsequent test that
    reuses the same ``channel_id``. Clear before AND after for safety.
    """
    dd.DispatchDeileTaskTool._LAST_DISPATCH.clear()
    yield
    dd.DispatchDeileTaskTool._LAST_DISPATCH.clear()


# ---------------------------------------------------------------------------
# _validate_bearer_token
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_value",
    [
        "abc\r\ndef",        # CRLF injection vector
        "abc\ndef",          # bare LF
        "abc\rdef",          # bare CR
        "abc\tdef",          # TAB (not in token68)
        "abc\x00def",        # NUL byte
        "abc def",           # plain space
        "abc\x7fdef",        # DEL (C0 control)
        "abc\x01def",        # SOH (C0 control)
        "",                  # empty string fails the 1*token68 requirement
        "héllo",             # non-ASCII
    ],
)
def test_validate_bearer_token_rejects_bad_values(bad_value):
    # Arrange / Act / Assert
    with pytest.raises(dd._DispatchConfigError):
        dd._validate_bearer_token(bad_value)


@pytest.mark.parametrize(
    "good_value",
    [
        "abcDEF123",                       # alphanumerics
        "abc-def_ghi.jkl~mno+pqr/stu",     # full token68 symbol set
        "ZXBlciBmcm9zdA==",                # base64 with padding
        "x",                               # minimal 1-char
        "A" * 256,                         # long
    ],
)
def test_validate_bearer_token_accepts_token68(good_value):
    # Arrange / Act
    result = dd._validate_bearer_token(good_value)
    # Assert
    assert result == good_value


# ---------------------------------------------------------------------------
# _worker_endpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "javascript:alert(1)",
        "://nohost",                # empty scheme
    ],
)
def test_worker_endpoint_rejects_non_http_schemes(monkeypatch, bad_url):
    # Arrange
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", bad_url)
    # Act / Assert
    with pytest.raises(dd._DispatchConfigError):
        dd._worker_endpoint()


def test_worker_endpoint_rejects_missing_netloc(monkeypatch):
    # Arrange — scheme is valid but there's no host
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http:///path-only")
    # Act / Assert
    with pytest.raises(dd._DispatchConfigError, match="missing host"):
        dd._worker_endpoint()


@pytest.mark.parametrize(
    "good_url",
    [
        "http://localhost:8766",
        "https://deile-worker.example.com",
        "http://10.0.0.1:8766/prefix",
    ],
)
def test_worker_endpoint_accepts_http_and_https(monkeypatch, good_url):
    # Arrange
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", good_url)
    # Act
    result = dd._worker_endpoint()
    # Assert
    assert result == good_url


# ---------------------------------------------------------------------------
# DispatchDeileTaskTool.execute — persona validation (defense-in-depth)
# ---------------------------------------------------------------------------


def _make_context(args, session_data=None):
    """Build a minimal ToolContext for invoking execute() directly."""
    return ToolContext(
        user_input="",
        parsed_args=args,
        session_data=session_data or {},
    )


async def test_execute_rejects_unknown_persona():
    # Arrange — bypasses JSON-Schema enum by calling execute() directly.
    tool = dd.DispatchDeileTaskTool()
    ctx = _make_context(
        {
            "brief": "do a thing",
            "channel_id": "C123",
            "persona": "evil-persona",
        }
    )
    # Act
    result = await tool.execute(ctx)
    # Assert
    assert result.is_error
    assert result.metadata["error_code"] == "BAD_REQUEST"
    assert "evil-persona" in result.message


@pytest.mark.parametrize("persona", list(dd._PERSONA_ALLOWLIST))
async def test_execute_accepts_allowlisted_persona(monkeypatch, persona):
    # Arrange — patch the network hop and token resolver so the test stays
    # offline and the cooldown record is exercised end-to-end.
    captured = {}

    async def _fake_post_dispatch(*, endpoint, payload, token, wait):
        captured["payload"] = payload
        return {"ok": True, "task_id": "T1", "elapsed_s": 0.1, "files": []}, None

    monkeypatch.setattr(dd, "_post_dispatch", _fake_post_dispatch)
    monkeypatch.setattr(dd, "_worker_token", lambda: "valid-token-abc123")
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "http://localhost:8766")

    tool = dd.DispatchDeileTaskTool()
    ctx = _make_context(
        {
            "brief": "do a thing",
            "channel_id": f"C-{persona}",  # unique per-param to avoid cooldown
            "persona": persona,
        }
    )
    # Act
    result = await tool.execute(ctx)
    # Assert
    assert result.is_success, result.message
    assert captured["payload"]["persona"] == persona


# ---------------------------------------------------------------------------
# DispatchDeileTaskTool.execute — config error mapping (minor 1)
# ---------------------------------------------------------------------------


async def test_execute_maps_dispatch_config_error_to_worker_config_invalid(monkeypatch):
    """Pin: invalid endpoint scheme must surface as WORKER_CONFIG_INVALID,
    not the generic INTERNAL_ERROR — they describe distinct failure modes
    to the caller LLM."""
    # Arrange
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "file:///etc/passwd")
    monkeypatch.setattr(dd, "_worker_token", lambda: "valid-token-abc123")

    tool = dd.DispatchDeileTaskTool()
    ctx = _make_context({"brief": "do thing", "channel_id": "C-config-err"})
    # Act
    result = await tool.execute(ctx)
    # Assert
    assert result.is_error
    assert result.metadata["error_code"] == "WORKER_CONFIG_INVALID"


async def test_execute_invalid_endpoint_does_not_burn_cooldown(monkeypatch):
    """Pin: when config validation fails, the channel's cooldown must NOT
    be recorded — otherwise a corrupted env var blocks the channel for 30s
    without ever attempting a real dispatch."""
    # Arrange
    monkeypatch.setenv("DEILE_WORKER_ENDPOINT", "file:///etc/passwd")
    monkeypatch.setattr(dd, "_worker_token", lambda: "valid-token-abc123")

    tool = dd.DispatchDeileTaskTool()
    ctx = _make_context({"brief": "do thing", "channel_id": "C-no-burn"})
    # Act
    result = await tool.execute(ctx)
    # Assert
    assert result.is_error
    assert "C-no-burn" not in dd.DispatchDeileTaskTool._LAST_DISPATCH
