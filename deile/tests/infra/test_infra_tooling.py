"""Testes das ferramentas de infra: _cli_ui, deploy.py, _service.py,
setup_environment.py e o workdir-por-canal do worker_server.py.

Os scripts de infra vivem fora do pacote `deile` (em `infra/`), então o
caminho é inserido no sys.path para o import. Só a lógica pura/robusta é
exercitada — instalar k3s ou subir serviços de verdade não é testável.
"""

from __future__ import annotations

import argparse
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


# ===== deploy.py — resolve_target (precedência) ==============================

def test_resolve_target_explicit_wins(monkeypatch):
    # --target explícito vence tudo, sem tocar deploy.json nem detectar nada.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: "local")
    monkeypatch.setattr(deploy, "namespace_exists", lambda: True)
    assert deploy.resolve_target("container") == "container"
    assert deploy.resolve_target("local") == "local"


def test_resolve_target_ignores_invalid_request(monkeypatch):
    # Valor inválido em --target é descartado; cai no deploy.json.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: "container")
    monkeypatch.setattr(deploy, "namespace_exists", lambda: False)
    assert deploy.resolve_target("garbage") == "container"


def test_resolve_target_saved_state(monkeypatch):
    # Sem --target, o deploy.json vence a auto-detecção.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: "local")
    monkeypatch.setattr(deploy, "namespace_exists", lambda: True)
    assert deploy.resolve_target(None) == "local"


def test_resolve_target_autodetect_container(monkeypatch):
    # Sem --target e sem deploy.json: namespace presente → container.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: None)
    monkeypatch.setattr(deploy, "namespace_exists", lambda: True)
    assert deploy.resolve_target(None) == "container"


def test_resolve_target_autodetect_local(monkeypatch):
    # Sem namespace, mas serviço local rodando → local.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: None)
    monkeypatch.setattr(deploy, "namespace_exists", lambda: False)

    class _FakeSvc:
        def __init__(self, *_a, **_kw):
            pass

        def status(self):
            return True, "rodando"

    monkeypatch.setattr(deploy, "LocalService", _FakeSvc)
    assert deploy.resolve_target(None) == "local"


def test_resolve_target_undetermined(monkeypatch):
    # Nada configurado e nada rodando → None.
    monkeypatch.setattr(deploy, "read_deploy_target", lambda: None)
    monkeypatch.setattr(deploy, "namespace_exists", lambda: False)

    class _FakeSvc:
        def __init__(self, *_a, **_kw):
            pass

        def status(self):
            return False, "parado"

    monkeypatch.setattr(deploy, "LocalService", _FakeSvc)
    assert deploy.resolve_target(None) is None


# ===== deploy.py — _image_build_cmd (seleção de runtime) =====================

def test_image_build_cmd_prefers_nerdctl(monkeypatch):
    monkeypatch.setattr(deploy, "_resolve", lambda t: "/usr/bin/nerdctl" if t == "nerdctl" else None)
    monkeypatch.setattr(deploy, "which", lambda t: None)
    cmd = deploy._image_build_cmd()
    assert cmd is not None
    assert cmd[0] == "/usr/bin/nerdctl"
    assert "--namespace" in cmd and "k8s.io" in cmd


def test_image_build_cmd_falls_back_to_colima(monkeypatch):
    monkeypatch.setattr(deploy, "_resolve", lambda t: None)
    monkeypatch.setattr(deploy, "which", lambda t: t in ("colima",))
    cmd = deploy._image_build_cmd()
    assert cmd is not None
    assert cmd[0] == "colima"
    assert cmd[1] == "nerdctl"


def test_image_build_cmd_falls_back_to_docker(monkeypatch):
    monkeypatch.setattr(deploy, "_resolve", lambda t: None)
    monkeypatch.setattr(deploy, "which", lambda t: t == "docker")
    cmd = deploy._image_build_cmd()
    assert cmd is not None
    assert cmd[0] == "docker"
    assert cmd[1] == "build"


def test_image_build_cmd_no_runtime(monkeypatch):
    monkeypatch.setattr(deploy, "_resolve", lambda t: None)
    monkeypatch.setattr(deploy, "which", lambda t: None)
    assert deploy._image_build_cmd() is None


# ===== setup_environment.py — _wants_container ===============================

def _ns(**kw) -> argparse.Namespace:
    base = {"mode": None, "yes": False}
    base.update(kw)
    return argparse.Namespace(**base)


def test_wants_container_mode_container():
    assert setup_environment._wants_container(_ns(mode="container")) is True


def test_wants_container_mode_local():
    assert setup_environment._wants_container(_ns(mode="local")) is False


def test_wants_container_yes_defaults_local():
    # --yes sem --mode: default não-interativo é ambiente local.
    assert setup_environment._wants_container(_ns(yes=True)) is False


def test_wants_container_interactive_prompt(monkeypatch):
    # Sem --mode e sem --yes: a decisão vem do ui.confirm.
    monkeypatch.setattr(setup_environment.ui, "confirm", lambda *a, **kw: True)
    assert setup_environment._wants_container(_ns()) is True
    monkeypatch.setattr(setup_environment.ui, "confirm", lambda *a, **kw: False)
    assert setup_environment._wants_container(_ns()) is False


# ===== _service.py — LocalService.backend (seleção de backend) ===============

def test_backend_macos_is_launchd(tmp_path, monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "darwin")
    assert _service.LocalService(tmp_path).backend == "launchd"


def test_backend_linux_with_systemd(tmp_path, monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service.LocalService, "_systemd_user_ok", staticmethod(lambda: True))
    assert _service.LocalService(tmp_path).backend == "systemd"


def test_backend_linux_without_systemd_is_pidfile(tmp_path, monkeypatch):
    monkeypatch.setattr(_service.sys, "platform", "linux")
    monkeypatch.setattr(_service.LocalService, "_systemd_user_ok", staticmethod(lambda: False))
    assert _service.LocalService(tmp_path).backend == "pidfile"


def test_backend_other_platform_is_pidfile(tmp_path, monkeypatch):
    # Plataforma não-Linux/não-macOS (ex.: win32) → pidfile.
    monkeypatch.setattr(_service.sys, "platform", "win32")
    assert _service.LocalService(tmp_path).backend == "pidfile"
