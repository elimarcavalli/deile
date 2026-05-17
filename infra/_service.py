"""Backend de serviço em segundo plano do deilebot (modo local, sem k8s).

Mantém o bot no ar 24/7 quando ele roda fora do Kubernetes. Três
backends, escolhidos automaticamente:

  - systemd  (Linux): unidade ``systemd --user`` com ``Restart=on-failure``;
                      ``loginctl enable-linger`` faz sobreviver ao logout.
  - launchd  (macOS): LaunchAgent com ``KeepAlive`` — reinicia se cair.
  - pidfile  (fallback): processo destacado (``setsid``) rastreado por um
                         pidfile + logfile; sobrevive ao logout, mas NÃO
                         reinicia sozinho se cair.

Importado pelo ``deploy.py`` para os comandos start/stop/restart/status/
logs no modo local. Apenas stdlib.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from shutil import which
from typing import List, Optional, Tuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _cli_ui as ui  # noqa: E402

_SYSTEMD_UNIT = "deilebot.service"
_LAUNCHD_LABEL = "com.deile.deilebot"


class LocalService:
    """Gerencia o bot como serviço de segundo plano numa máquina sem k8s."""

    def __init__(self, root: Path, python: Optional[str] = None) -> None:
        self.root = Path(root)
        self.python = python or sys.executable
        self.state_dir = self.root / ".deile"
        self.pidfile = self.state_dir / "deilebot.pid"
        self.logfile = self.state_dir / "deilebot.local.log"

    @property
    def bot_cmd(self) -> List[str]:
        return [self.python, "-m", "deilebot", "run", "--provider", "discord"]

    @property
    def backend(self) -> str:
        if sys.platform == "darwin":
            return "launchd"
        if sys.platform.startswith("linux") and self._systemd_user_ok():
            return "systemd"
        return "pidfile"

    # ----- detecção -----

    @staticmethod
    def _systemd_user_ok() -> bool:
        if which("systemctl") is None:
            return False
        try:
            r = subprocess.run(
                ["systemctl", "--user", "is-system-running"],
                capture_output=True, timeout=10,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        # is-system-running pode sair != 0 ("degraded") e ainda assim o
        # gerenciador de usuário estar de pé. Falha real = não conectou ao bus.
        return b"Failed to connect" not in (r.stderr or b"")

    # ----- API pública (despacha pelo backend) -----

    def start(self) -> bool:
        ui.info(f"backend de serviço: {self.backend}")
        return {
            "systemd": self._start_systemd,
            "launchd": self._start_launchd,
            "pidfile": self._start_pidfile,
        }[self.backend]()

    def stop(self) -> bool:
        return {
            "systemd": self._stop_systemd,
            "launchd": self._stop_launchd,
            "pidfile": self._stop_pidfile,
        }[self.backend]()

    def restart(self) -> bool:
        self.stop()
        return self.start()

    def status(self) -> Tuple[bool, str]:
        """(rodando?, descrição)."""
        return {
            "systemd": self._status_systemd,
            "launchd": self._status_launchd,
            "pidfile": self._status_pidfile,
        }[self.backend]()

    def logs(self, lines: int = 80) -> None:
        if self.backend == "systemd":
            subprocess.run([
                "journalctl", "--user", "-u", _SYSTEMD_UNIT,
                "-n", str(lines), "--no-pager",
            ])
        else:
            self._tail_logfile(lines)

    # ----- systemd -----

    def _systemd_unit_path(self) -> Path:
        return Path.home() / ".config" / "systemd" / "user" / _SYSTEMD_UNIT

    def render_systemd_unit(self) -> str:
        """Conteúdo da unidade systemd (puro — testável)."""
        return (
            "[Unit]\n"
            "Description=deilebot — bot de mensageria do DEILE\n"
            "After=network-online.target\n\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={self.root}\n"
            f"ExecStart={self.python} -m deilebot run --provider discord\n"
            "Restart=on-failure\n"
            "RestartSec=5\n\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def _write_systemd_unit(self) -> None:
        path = self._systemd_unit_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_systemd_unit(), encoding="utf-8")
        ui.detail(f"unidade: {path}")

    def _start_systemd(self) -> bool:
        self._write_systemd_unit()
        subprocess.run(["systemctl", "--user", "daemon-reload"])
        r = subprocess.run(
            ["systemctl", "--user", "enable", "--now", _SYSTEMD_UNIT]
        )
        if r.returncode != 0:
            ui.err("`systemctl --user enable --now` falhou")
            return False
        user = os.environ.get("USER", "")
        linger = subprocess.run(
            ["loginctl", "enable-linger", user], capture_output=True
        )
        if linger.returncode == 0:
            ui.ok("linger habilitado — o bot sobrevive ao logout")
        else:
            ui.warn(
                "não consegui habilitar o linger automaticamente; "
                f"rode: sudo loginctl enable-linger {user}"
            )
        ui.ok("serviço iniciado (systemd --user)")
        return True

    def _stop_systemd(self) -> bool:
        r = subprocess.run(
            ["systemctl", "--user", "disable", "--now", _SYSTEMD_UNIT]
        )
        if r.returncode == 0:
            ui.ok("serviço parado")
            return True
        ui.err("`systemctl --user disable --now` falhou")
        return False

    def _status_systemd(self) -> Tuple[bool, str]:
        r = subprocess.run(
            ["systemctl", "--user", "is-active", _SYSTEMD_UNIT],
            capture_output=True, text=True,
        )
        state = (r.stdout or "").strip() or "desconhecido"
        return r.returncode == 0, f"systemd: {state}"

    # ----- launchd -----

    def _launchd_plist_path(self) -> Path:
        return Path.home() / "Library" / "LaunchAgents" / f"{_LAUNCHD_LABEL}.plist"

    def render_launchd_plist(self) -> str:
        """Conteúdo do LaunchAgent plist (puro — testável)."""
        args = "".join(f"        <string>{a}</string>\n" for a in self.bot_cmd)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
            '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0">\n<dict>\n'
            f"    <key>Label</key><string>{_LAUNCHD_LABEL}</string>\n"
            "    <key>ProgramArguments</key>\n    <array>\n"
            f"{args}    </array>\n"
            f"    <key>WorkingDirectory</key><string>{self.root}</string>\n"
            "    <key>RunAtLoad</key><true/>\n"
            "    <key>KeepAlive</key><true/>\n"
            f"    <key>StandardOutPath</key><string>{self.logfile}</string>\n"
            f"    <key>StandardErrorPath</key><string>{self.logfile}</string>\n"
            "</dict>\n</plist>\n"
        )

    def _write_launchd_plist(self) -> None:
        path = self._launchd_plist_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(self.render_launchd_plist(), encoding="utf-8")
        ui.detail(f"LaunchAgent: {path}")

    def _start_launchd(self) -> bool:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        plist = self._launchd_plist_path()
        self._write_launchd_plist()
        # Recarrega: unload defensivo, depois load -w (habilita no boot).
        subprocess.run(
            ["launchctl", "unload", str(plist)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        r = subprocess.run(["launchctl", "load", "-w", str(plist)])
        if r.returncode != 0:
            ui.err("`launchctl load` falhou")
            return False
        ui.ok("serviço iniciado (launchd) — reinicia sozinho se cair")
        return True

    def _stop_launchd(self) -> bool:
        plist = self._launchd_plist_path()
        if not plist.is_file():
            ui.warn("nenhum LaunchAgent instalado")
            return True
        r = subprocess.run(["launchctl", "unload", str(plist)])
        if r.returncode == 0:
            ui.ok("serviço parado")
            return True
        ui.err("`launchctl unload` falhou")
        return False

    def _status_launchd(self) -> Tuple[bool, str]:
        r = subprocess.run(
            ["launchctl", "list", _LAUNCHD_LABEL],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            return False, "launchd: não carregado"
        # A saída do `launchctl list <label>` traz "PID" = <n> se ativo.
        for line in (r.stdout or "").splitlines():
            stripped = line.strip()
            if stripped.startswith('"PID"'):
                pid = stripped.split("=")[-1].strip().rstrip(";").strip()
                if pid.isdigit():
                    return True, f"launchd: rodando (pid {pid})"
                return False, "launchd: carregado, processo parado"
        return True, "launchd: carregado"

    # ----- pidfile (fallback) -----

    def _read_pid(self) -> Optional[int]:
        try:
            return int(self.pidfile.read_text(encoding="utf-8").strip())
        except (OSError, ValueError):
            return None

    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (OSError, ProcessLookupError):
            return False
        return True

    def _pidfile_running(self) -> bool:
        pid = self._read_pid()
        return pid is not None and self._pid_alive(pid)

    def _start_pidfile(self) -> bool:
        if self._pidfile_running():
            ui.warn("o bot já parece estar rodando (pidfile)")
            return True
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            log = open(self.logfile, "a", encoding="utf-8")
        except OSError as exc:
            ui.err(f"não consegui abrir o logfile: {exc}")
            return False
        try:
            # start_new_session destaca o processo do terminal (setsid):
            # sobrevive a fechar o terminal / cair o SSH.
            proc = subprocess.Popen(
                self.bot_cmd, cwd=str(self.root),
                stdout=log, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, start_new_session=True,
            )
        except OSError as exc:
            ui.err(f"não consegui iniciar o bot: {exc}")
            return False
        finally:
            log.close()
        self.pidfile.write_text(str(proc.pid), encoding="utf-8")
        ui.ok(f"bot iniciado em segundo plano (pid {proc.pid})")
        ui.warn(
            "backend pidfile: o bot NÃO reinicia sozinho se cair "
            "(systemd/launchd dariam isso)"
        )
        return True

    def _stop_pidfile(self) -> bool:
        import signal as _signal

        pid = self._read_pid()
        if pid is None or not self._pid_alive(pid):
            ui.warn("o bot não está rodando")
            self.pidfile.unlink(missing_ok=True)
            return True
        try:
            os.kill(pid, _signal.SIGTERM)
        except OSError as exc:
            ui.err(f"não consegui parar o processo {pid}: {exc}")
            return False
        self.pidfile.unlink(missing_ok=True)
        ui.ok(f"bot parado (pid {pid})")
        return True

    def _status_pidfile(self) -> Tuple[bool, str]:
        pid = self._read_pid()
        if pid is None:
            return False, "pidfile: parado"
        if self._pid_alive(pid):
            return True, f"pidfile: rodando (pid {pid})"
        return False, "pidfile: parado (pidfile obsoleto)"

    # ----- logs -----

    def _tail_logfile(self, lines: int) -> None:
        if not self.logfile.is_file():
            ui.warn(f"sem logfile ainda ({self.logfile})")
            return
        try:
            content = self.logfile.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            ui.err(f"não consegui ler o logfile: {exc}")
            return
        for line in content.splitlines()[-lines:]:
            print(line)
