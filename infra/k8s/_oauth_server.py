#!/usr/bin/env python3
"""In-pod OAuth relay server for ``deploy.py k8s claude-login --in-pod`` (issue #335).

Runs a minimal HTTP server on port 9876 (no external deps — stdlib only) that
acts as a relay between the operator's browser (accessed via kubectl port-forward)
and the ``claude auth login`` OAuth flow inside the pod.

Why a relay is needed
---------------------
``claude auth login`` generates an ephemeral localhost callback URL (e.g.
``http://localhost:XXXX/callback``) for the OAuth redirect.  In-pod, that URL
is unreachable from the operator's browser.  This server:

  1. Starts ``claude auth login`` as a subprocess (without opening a browser).
  2. Extracts the OAuth URL + ephemeral callback port from the CLI output.
  3. Rewrites the ``redirect_uri`` parameter to ``http://localhost:9876/auth/callback``
     so Anthropic redirects back to THIS server (which the operator has port-forwarded).
  4. Proxies the callback from the operator's browser to claude's local listener
     (same network namespace, in-pod, reachable).

Usage (inside the claude-worker pod)::

    python3 /app/infra/k8s/_oauth_server.py

Operators access ``http://localhost:9876/auth/start`` via::

    kubectl port-forward deploy/claude-worker 9876:9876

Endpoints
---------
GET /auth/start    → HTML page; triggers claude auth login, shows OAuth URL
GET /auth/status   → JSON ``{"authenticated": bool, "email": str|null}``
GET /auth/callback → Proxies Anthropic's callback to claude's local listener
GET /health        → JSON ``{"ok": true}`` (readiness probe)
"""
from __future__ import annotations

import http.server
import json
import os
import re
import subprocess
import threading
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional, Tuple

_PORT = int(os.environ.get("CLAUDE_OAUTH_SERVER_PORT", "9876"))
_CREDS_PATH = Path(os.environ.get("HOME", "/home/claude")) / ".claude" / "credentials.json"

_lock = threading.Lock()
_state: dict = {
    "proc": None,           # subprocess.Popen | None
    "oauth_url": None,      # str | None — original URL from claude output
    "callback_port": None,  # int | None — claude's ephemeral listener port
    "done": False,
    "error": None,
}

_HTML = """\
<!DOCTYPE html><html><head><meta charset="utf-8">
<title>DEILE — Claude Login</title>
<style>
body{{font-family:monospace;max-width:820px;margin:48px auto;padding:20px;color:#222}}
h2{{color:#1a1a2e}}
a{{color:#4a90d9;word-break:break-all}}
pre{{background:#f5f5f5;padding:12px;border-radius:4px;overflow-x:auto;white-space:pre-wrap}}
.ok{{color:#2e7d32}}.err{{color:#c62828}}.warn{{color:#e65100}}
</style></head>
<body><h2>DEILE — Claude OAuth In-Pod</h2>
{body}
</body></html>"""


def _auth_status() -> dict:
    """Return ``{"authenticated": bool, "email": str|null}``."""
    if not _CREDS_PATH.exists():
        return {"authenticated": False, "email": None}
    try:
        data = json.loads(_CREDS_PATH.read_text())
    except Exception:
        return {"authenticated": False, "email": None}
    email: Optional[str] = None
    if isinstance(data, dict):
        email = data.get("email")
        if not email and isinstance(data.get("claudeAiOauth"), dict):
            email = data["claudeAiOauth"].get("email")
    return {"authenticated": True, "email": email}


def _extract_url_and_port(line: str) -> Tuple[Optional[str], Optional[int]]:
    """Parse a line of ``claude auth login`` output.

    Returns ``(oauth_url, callback_port)`` when the line contains a URL
    with a ``redirect_uri`` param (callback port embedded), else ``(None, None)``.
    """
    m = re.search(r'https?://\S+', line)
    if not m:
        return None, None
    url = m.group(0).rstrip(".,;'\")")
    parsed = urllib.parse.urlparse(url)
    qs = urllib.parse.parse_qs(parsed.query)
    redirect_list = qs.get("redirect_uri", [])
    if not redirect_list:
        # URL present but no redirect_uri — return URL only, port unknown yet
        return url, None
    redirect_uri = urllib.parse.unquote(redirect_list[0])
    port_m = re.search(r'localhost:(\d+)', redirect_uri)
    port = int(port_m.group(1)) if port_m else None
    return url, port


def _rewrite_redirect_uri(oauth_url: str, relay_port: int) -> str:
    """Replace ``redirect_uri`` in *oauth_url* to route through this server."""
    parsed = urllib.parse.urlparse(oauth_url)
    qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    if "redirect_uri" not in qs:
        return oauth_url
    qs["redirect_uri"] = [f"http://localhost:{relay_port}/auth/callback"]
    return urllib.parse.urlunparse(parsed._replace(
        query=urllib.parse.urlencode(qs, doseq=True),
    ))


def _run_claude_login() -> None:
    """Background thread: spawn ``claude auth login``, parse output."""
    env = os.environ.copy()
    # Remove display-related vars so claude doesn't attempt to open a browser
    # inside the pod (which has no GUI).  The operator opens the URL manually.
    for var in ("DISPLAY", "WAYLAND_DISPLAY", "BROWSER"):
        env.pop(var, None)
    try:
        proc = subprocess.Popen(
            ["claude", "auth", "login"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1,
        )
    except FileNotFoundError:
        with _lock:
            _state["error"] = "claude CLI não encontrado no PATH"
        return

    with _lock:
        _state["proc"] = proc

    try:
        assert proc.stdout is not None
        for raw_line in proc.stdout:
            line = raw_line.rstrip()
            url, port = _extract_url_and_port(line)
            with _lock:
                if url and _state["oauth_url"] is None:
                    _state["oauth_url"] = url
                if port and _state["callback_port"] is None:
                    _state["callback_port"] = port
        proc.wait()
        with _lock:
            if proc.returncode == 0:
                _state["done"] = True
            elif _state["error"] is None:
                _state["error"] = f"claude auth login saiu com código {proc.returncode}"
    except Exception as exc:  # noqa: BLE001
        with _lock:
            _state["error"] = str(exc)


class _Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args) -> None:  # type: ignore[override]
        pass  # silence default request log — pod stdout should stay clean

    def _html(self, body: str, code: int = 200) -> None:
        payload = _HTML.format(body=body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def _json(self, data: dict, code: int = 200) -> None:
        payload = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"

        if path == "/auth/start":
            self._handle_start()
        elif path == "/auth/status":
            self._json(_auth_status())
        elif path.startswith("/auth/callback"):
            self._handle_callback(parsed)
        elif path == "/health":
            self._json({"ok": True})
        else:
            self._html(
                "<p>Endpoints disponíveis:</p><ul>"
                "<li><a href='/auth/start'>/auth/start</a> — iniciar OAuth</li>"
                "<li><a href='/auth/status'>/auth/status</a> — verificar status</li>"
                "<li><a href='/health'>/health</a> — readiness</li>"
                "</ul>"
            )

    # ------------------------------------------------------------------
    # Route handlers
    # ------------------------------------------------------------------

    def _handle_start(self) -> None:
        status = _auth_status()
        if status["authenticated"]:
            email = status["email"] or "conta desconhecida"
            self._html(f"<p class='ok'>✅ Já autenticado como <b>{email}</b>.</p>")
            return

        with _lock:
            already = _state["proc"] is not None
            url = _state["oauth_url"]
            done = _state["done"]
            error = _state["error"]

        if done:
            self._html("<p class='ok'>✅ Autenticação concluída. Verifique "
                       "<a href='/auth/status'>/auth/status</a>.</p>")
            return
        if error:
            self._html(
                f"<p class='err'>❌ Erro: <pre>{error}</pre>"
                "Reinicie o servidor e tente novamente.</p>",
                code=500,
            )
            return
        if not already:
            threading.Thread(target=_run_claude_login, daemon=True).start()
            self._html(
                "<p>⏳ Iniciando OAuth in-pod…"
                " <b>Recarregue esta página em 3 segundos.</b></p>"
                "<meta http-equiv='refresh' content='3;url=/auth/start'>",
            )
            return
        if url is None:
            self._html(
                "<p>⏳ Aguardando URL do OAuth do claude CLI…"
                " <b>Recarregue em 2 segundos.</b></p>"
                "<meta http-equiv='refresh' content='2;url=/auth/start'>",
            )
            return

        relay_url = _rewrite_redirect_uri(url, _PORT)
        self._html(
            "<p>Clique no link abaixo para autenticar:</p>"
            f"<p><a href='{relay_url}'>{relay_url}</a></p>"
            "<p>Após completar o OAuth no browser, "
            "<a href='/auth/status'>verifique o status</a>.</p>"
            "<p><small>URL original do claude CLI:"
            f"<pre>{url}</pre></small></p>"
        )

    def _handle_callback(self, parsed: urllib.parse.ParseResult) -> None:
        """Forward Anthropic's OAuth callback to claude's local listener."""
        with _lock:
            callback_port = _state["callback_port"]

        if not callback_port:
            self._html(
                "<p class='warn'>⚠️ Porta de callback ainda não detectada. "
                "Aguarde alguns segundos e tente novamente.</p>",
                code=503,
            )
            return

        query = parsed.query
        target = f"http://127.0.0.1:{callback_port}/callback?{query}"
        try:
            with urllib.request.urlopen(target, timeout=15) as resp:
                resp.read()
            self._html(
                "<p class='ok'>✅ Callback recebido e repassado ao Claude CLI.</p>"
                "<p><a href='/auth/status'>Verificar status</a></p>"
            )
        except Exception as exc:  # noqa: BLE001
            self._html(
                f"<p class='warn'>⚠️ Callback não pôde ser repassado ao Claude CLI: "
                f"<pre>{exc}</pre>"
                "Se o OAuth foi completado com sucesso no browser, "
                "<a href='/auth/status'>verifique o status</a> mesmo assim.</p>"
            )


def main() -> None:
    server = http.server.HTTPServer(("0.0.0.0", _PORT), _Handler)
    print(f"DEILE OAuth relay :{_PORT} — acesse http://localhost:{_PORT}/auth/start",
          flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
