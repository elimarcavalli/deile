"""Tests for infra/k8s/_oauth_server.py — in-pod OAuth relay (issue #335).

The module is stdlib-only and runs inside the claude-worker pod.  These
tests verify the URL-parsing helpers and the HTTP handler logic without
actually starting a subprocess or binding a socket.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
import tempfile
from http import HTTPStatus
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_SERVER_PATH = (
    Path(__file__).resolve().parents[3] / "infra" / "k8s" / "_oauth_server.py"
)


def _load_server():
    """Load _oauth_server module without executing main()."""
    spec = importlib.util.spec_from_file_location("_oauth_server", _SERVER_PATH)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture()
def srv():
    return _load_server()


# ---------------------------------------------------------------------------
# _extract_url_and_port
# ---------------------------------------------------------------------------

class TestExtractUrlAndPort:
    def test_url_with_redirect_uri_and_port(self, srv):
        line = (
            "Please open: https://claude.ai/oauth/authorize"
            "?client_id=abc&redirect_uri=http%3A%2F%2Flocalhost%3A54321%2Fcallback"
        )
        url, port = srv._extract_url_and_port(line)
        assert url is not None
        assert "claude.ai" in url
        assert port == 54321

    def test_url_without_redirect_uri(self, srv):
        line = "Please visit https://example.com/oauth to continue"
        url, port = srv._extract_url_and_port(line)
        assert url == "https://example.com/oauth"
        assert port is None

    def test_no_url(self, srv):
        url, port = srv._extract_url_and_port("no url here — just text")
        assert url is None
        assert port is None

    def test_strips_trailing_punctuation(self, srv):
        line = "Open https://example.com/path."
        url, port = srv._extract_url_and_port(line)
        assert url == "https://example.com/path"

    def test_redirect_uri_decoded(self, srv):
        """Percent-encoded redirect_uri is decoded to extract port."""
        line = (
            "https://api.anthropic.com/oauth/authorize"
            "?redirect_uri=http%3A//localhost%3A9999/cb"
        )
        url, port = srv._extract_url_and_port(line)
        assert port == 9999


# ---------------------------------------------------------------------------
# _rewrite_redirect_uri
# ---------------------------------------------------------------------------

class TestRewriteRedirectUri:
    def test_rewrites_to_relay_port(self, srv):
        import urllib.parse  # noqa: PLC0415
        original = (
            "https://claude.ai/oauth/authorize"
            "?client_id=x&redirect_uri=http%3A%2F%2Flocalhost%3A54321%2Fcallback"
        )
        rewritten = srv._rewrite_redirect_uri(original, relay_port=9876)
        # The redirect_uri value may be percent-encoded — decode before asserting
        decoded = urllib.parse.unquote(rewritten)
        assert "localhost:9876" in decoded
        assert "/auth/callback" in decoded
        # Original port must be gone from redirect_uri
        assert "54321" not in decoded.split("redirect_uri=", 1)[-1]

    def test_no_redirect_uri_returns_original(self, srv):
        original = "https://claude.ai/oauth/authorize?client_id=x"
        assert srv._rewrite_redirect_uri(original, relay_port=9876) == original


# ---------------------------------------------------------------------------
# _auth_status
# ---------------------------------------------------------------------------

class TestAuthStatus:
    def test_no_credentials_file(self, srv, tmp_path):
        with patch.object(srv, "_CREDS_PATH", tmp_path / "nonexistent.json"):
            status = srv._auth_status()
        assert status == {"authenticated": False, "email": None}

    def test_with_valid_credentials_file(self, srv, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({
            "claudeAiOauth": {"accessToken": "tok", "email": "user@example.com"},
        }))
        with patch.object(srv, "_CREDS_PATH", creds):
            status = srv._auth_status()
        assert status["authenticated"] is True
        assert status["email"] == "user@example.com"

    def test_with_email_at_root_level(self, srv, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"email": "root@example.com", "token": "t"}))
        with patch.object(srv, "_CREDS_PATH", creds):
            status = srv._auth_status()
        assert status["authenticated"] is True
        assert status["email"] == "root@example.com"

    def test_invalid_json_returns_not_authenticated(self, srv, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text("this is not json {{{")
        with patch.object(srv, "_CREDS_PATH", creds):
            status = srv._auth_status()
        assert status["authenticated"] is False


# ---------------------------------------------------------------------------
# HTTP handler — _Handler
# ---------------------------------------------------------------------------

def _make_handler(srv, path: str, module_overrides: dict | None = None):
    """Instantiate _Handler with a fake request for the given path."""
    handler = srv._Handler.__new__(srv._Handler)
    handler.path = path
    handler.headers = {}
    buf = io.BytesIO()
    handler.wfile = buf
    handler._response_code = None
    handler._response_headers = []

    def fake_send_response(code):
        handler._response_code = code

    def fake_send_header(k, v):
        handler._response_headers.append((k, v))

    def fake_end_headers():
        pass

    handler.send_response = fake_send_response
    handler.send_header = fake_send_header
    handler.end_headers = fake_end_headers

    # Apply overrides to the module (restored after each use)
    return handler


class TestHandler:
    def _call(self, srv, path: str, state_overrides: dict | None = None):
        """Call do_GET on a _Handler for path, optionally patching _state."""
        base_state = {
            "proc": None,
            "oauth_url": None,
            "callback_port": None,
            "done": False,
            "error": None,
        }
        if state_overrides:
            base_state.update(state_overrides)

        h = _make_handler(srv, path)
        with patch.object(srv, "_state", base_state):
            h.do_GET()

        response = h.wfile.getvalue().decode()
        return h._response_code, response

    def test_health_returns_ok_json(self, srv):
        code, body = self._call(srv, "/health")
        assert code == 200
        assert "ok" in body.lower() or "true" in body.lower()

    def test_status_unauthenticated(self, srv, tmp_path):
        with patch.object(srv, "_CREDS_PATH", tmp_path / "nope.json"):
            code, body = self._call(srv, "/auth/status")
        assert code == 200
        data = json.loads(body)
        assert data["authenticated"] is False

    def test_start_already_authenticated(self, srv, tmp_path):
        creds = tmp_path / "credentials.json"
        creds.write_text(json.dumps({"claudeAiOauth": {"email": "u@e.com"}}))
        with patch.object(srv, "_CREDS_PATH", creds):
            code, body = self._call(srv, "/auth/start")
        assert code == 200
        assert "u@e.com" in body

    def test_start_triggers_login_when_proc_is_none(self, srv, tmp_path):
        with patch.object(srv, "_CREDS_PATH", tmp_path / "nope.json"):
            launched = []

            def fake_thread(**kwargs):
                t = MagicMock()
                t.start = lambda: launched.append(True)
                return t

            import threading  # noqa: PLC0415
            with patch("threading.Thread", side_effect=fake_thread):
                code, body = self._call(srv, "/auth/start", {"proc": None})
        assert code == 200
        # Should show "reload" / waiting message
        assert "reload" in body.lower() or "aguard" in body.lower() or "iniciand" in body.lower()

    def test_start_shows_oauth_url_when_available(self, srv, tmp_path):
        with patch.object(srv, "_CREDS_PATH", tmp_path / "nope.json"):
            fake_proc = MagicMock()
            state = {
                "proc": fake_proc,
                "oauth_url": "https://claude.ai/oauth?redirect_uri=http%3A//localhost%3A1234/cb",
                "callback_port": 1234,
                "done": False,
                "error": None,
            }
            code, body = self._call(srv, "/auth/start", state)
        assert code == 200
        assert "claude.ai" in body
        # relay URL should appear with port 9876
        assert "9876" in body or "localhost" in body

    def test_start_error_state(self, srv, tmp_path):
        with patch.object(srv, "_CREDS_PATH", tmp_path / "nope.json"):
            fake_proc = MagicMock()
            state = {
                "proc": fake_proc,
                "oauth_url": None,
                "callback_port": None,
                "done": False,
                "error": "claude não encontrado",
            }
            code, body = self._call(srv, "/auth/start", state)
        assert code == 500
        assert "claude não encontrado" in body

    def test_callback_without_port(self, srv):
        state = {
            "proc": MagicMock(),
            "oauth_url": "https://x.com",
            "callback_port": None,
            "done": False,
            "error": None,
        }
        code, body = self._call(srv, "/auth/callback?code=abc", state)
        assert code == 503

    def test_unknown_path_returns_200_with_links(self, srv):
        code, body = self._call(srv, "/unknown-route")
        assert code == 200
        assert "/auth/start" in body
