"""Testes das ferramentas de infra: _cli_ui, deploy.py, _service.py,
setup_environment.py e o workdir-por-canal do worker_server.py.

Os scripts de infra vivem fora do pacote `deile` (em `infra/`), então o
caminho é inserido no sys.path para o import. Só a lógica pura/robusta é
exercitada — instalar k3s ou subir serviços de verdade não é testável.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[3]
for _p in (_REPO / "infra", _REPO / "infra" / "k8s"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import _cli_ui  # noqa: E402
import _service  # noqa: E402
import deploy  # noqa: E402
import setup_environment  # noqa: E402


# ===== _cli_ui ===============================================================

def test_paint_with_color_wraps_ansi():
    _cli_ui.set_color(True)
    try:
        out = _cli_ui.paint("oi", "red", "bold")
        assert "\033[" in out
        assert "oi" in out
        assert out.endswith("\033[0m")
    finally:
        _cli_ui.set_color(False)


def test_paint_without_color_is_plain():
    _cli_ui.set_color(False)
    assert _cli_ui.paint("oi", "red", "bold") == "oi"


def test_set_color_toggles_state():
    _cli_ui.set_color(True)
    assert _cli_ui.color_enabled() is True
    _cli_ui.set_color(False)
    assert _cli_ui.color_enabled() is False


# ===== deploy.py — parse_args ================================================

def test_parse_args_empty_is_help():
    assert deploy.parse_args([])["command"] == "help"


def test_parse_args_simple_command():
    args = deploy.parse_args(["up"])
    assert args["command"] == "up"
    assert args["extra"] == []


def test_parse_args_positional_extra():
    args = deploy.parse_args(["clone", "elimarcavalli/deile"])
    assert args["command"] == "clone"
    assert args["extra"] == ["elimarcavalli/deile"]


def test_parse_args_target_flag():
    args = deploy.parse_args(["status", "--target", "local"])
    assert args["command"] == "status"
    assert args["target"] == "local"


def test_parse_args_boolean_flags():
    args = deploy.parse_args(["reset", "--yes", "--rebuild"])
    assert args["command"] == "reset"
    assert args["yes"] is True
    assert args["rebuild"] is True


def test_parse_args_help_flag():
    assert deploy.parse_args(["--help"])["command"] == "help"
    assert deploy.parse_args(["--no-color", "logs"])["no_color"] is True


# ===== deploy.py — .env e deploy-state =======================================

def test_read_env_parses_pairs(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "# comentário\n"
        'DEILE_BOT_DISCORD_TOKEN="tok123"\n'
        "export OPENAI_API_KEY=sk-abc\n"
        "VAZIO=\n"
    )
    monkeypatch.setattr(deploy, "ENV_FILE", env)
    data = deploy.read_env()
    assert data["DEILE_BOT_DISCORD_TOKEN"] == "tok123"
    assert data["OPENAI_API_KEY"] == "sk-abc"
    assert data["VAZIO"] == ""


def test_read_env_missing_file(tmp_path, monkeypatch):
    monkeypatch.setattr(deploy, "ENV_FILE", tmp_path / "inexistente.env")
    assert deploy.read_env() == {}


def test_deploy_target_round_trip(tmp_path, monkeypatch):
    state = tmp_path / ".deile" / "deploy.json"
    monkeypatch.setattr(deploy, "DEPLOY_STATE", state)
    assert deploy.read_deploy_target() is None
    deploy.write_deploy_target("container")
    assert deploy.read_deploy_target() == "container"
    deploy.write_deploy_target("local")
    assert deploy.read_deploy_target() == "local"


def test_deploy_target_rejects_garbage(tmp_path, monkeypatch):
    state = tmp_path / ".deile" / "deploy.json"
    state.parent.mkdir(parents=True)
    state.write_text('{"target": "nonsense"}')
    monkeypatch.setattr(deploy, "DEPLOY_STATE", state)
    assert deploy.read_deploy_target() is None


# ===== _service.py — LocalService ============================================

def test_local_service_bot_cmd(tmp_path):
    svc = _service.LocalService(tmp_path, python="/opt/py/python3")
    assert svc.bot_cmd == [
        "/opt/py/python3", "-m", "deilebot", "run", "--provider", "discord",
    ]


def test_render_systemd_unit(tmp_path):
    svc = _service.LocalService(tmp_path, python="/opt/py/python3")
    unit = svc.render_systemd_unit()
    assert "[Service]" in unit
    assert f"WorkingDirectory={tmp_path}" in unit
    assert "/opt/py/python3 -m deilebot run --provider discord" in unit
    assert "Restart=on-failure" in unit


def test_render_launchd_plist(tmp_path):
    svc = _service.LocalService(tmp_path, python="/opt/py/python3")
    plist = svc.render_launchd_plist()
    assert "com.deile.deilebot" in plist
    assert "<key>KeepAlive</key><true/>" in plist
    assert "<string>/opt/py/python3</string>" in plist
    assert "<string>deilebot</string>" in plist


def test_local_service_pid_helpers(tmp_path):
    svc = _service.LocalService(tmp_path)
    assert svc._read_pid() is None
    svc.state_dir.mkdir(parents=True, exist_ok=True)
    svc.pidfile.write_text("4242")
    assert svc._read_pid() == 4242
    assert svc._pid_alive(os.getpid()) is True
    assert svc._pid_alive(2_000_000_000) is False


# ===== setup_environment.py ==================================================

def test_osinfo_detects_current_platform():
    info = setup_environment.OSInfo()
    assert info.system in ("Linux", "Darwin", "Windows")
    assert info.system in info.label
    assert sum((info.is_linux, info.is_macos, info.is_windows)) == 1


def test_module_available():
    assert setup_environment._module_available("os") is True
    assert setup_environment._module_available("modulo_que_nao_existe_xyz") is False


# ===== worker_server.py — workdir por canal ==================================

def test_channel_workdir_sanitization():
    pytest.importorskip("aiohttp")
    import worker_server

    # Snowflake do Discord (só dígitos) passa intacto.
    assert worker_server._channel_workdir("123456789012345678") == "123456789012345678"
    # Caracteres de path traversal são removidos.
    assert worker_server._channel_workdir("../../etc/passwd") == "etcpasswd"
    # Vazio cai no default seguro.
    assert worker_server._channel_workdir("") == "default"
    assert worker_server._channel_workdir(None) == "default"
    # Hífen e underscore são preservados.
    assert worker_server._channel_workdir("abc-DEF_12") == "abc-DEF_12"
