"""View dedicada ao deile-monitor — tela cheia acessada por [M] (MAIÚSCULO).

Exibe estado operacional completo do pod supervisor:
- Cabeçalho com info do pod e próximo tick
- Bloco CONFIG (envs aplicadas no Deployment)
- Tabela VIGIAS V1-V7 (inferida do audit log)
- Tabela ANOMALIAS ATIVAS (do monitor-state.json)
- Último tick executado
- Audit log (últimas 15 linhas)
- Log de notificações (últimas 5 linhas)

Hotkeys locais:
  t  força tick imediato  (rm /state/monitor-state.json)
  p  pause com submenu duração
  r  resume               (rm /state/monitor-pause)
  a  ack fingerprint      (escreve em monitor-state.json via exec)
  i  editar tick interval (kubectl set env)
  u  set notify user-id   (echo > /state/notify-user-id via exec)
  m  picker de model      (kubectl set env DEILE_PREFERRED_MODEL)
  k  stop (scale to 0) com confirm
  s  start / install com confirm
  l  tail do audit log (Live full-screen, ESC para sair)
  esc / q  voltar ao dashboard

ATENÇÃO: a tecla de entrada nesta view é [M] MAIÚSCULO.
Isso garante que não conflite com [m] minúsculo que já é model-switcher.
O KeyReader distingue maiúsculo/minúsculo — KeyReader.read() devolve a
letra exatamente como veio do terminal. Portanto no DashboardView.handle_key
mapeamos "M" → "monitor-view".
"""

from __future__ import annotations

import json
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich import box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Import local (unqualified) — mesmo padrão de _panel.py.
# deploy.py insere infra/k8s/ no sys.path antes de importar.
import _panel_data as pd_mod

# Regex para extrair marcadores de vigia do audit log.
# Formato esperado pela persona: "<ts> ACTION/SKIP/NOTIFY <fingerprint> <detalhe>"
# e presença de "V1"..."V7" no body da linha.
_VIGIA_RE = re.compile(r"\bV([1-7])\b")
_AUDIT_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[^\s]*)\s+(.*)$")

_VIGIA_NAMES = {
    1: "OAuth expirado",
    2: "Pods em erro",
    3: "Issues órfãs",
    4: "PRs attempt N/3",
    5: "Aguard. stakeholder",
    6: "BackoffLimitExceeded",
    7: "Pipeline não-saudável",
}

_UTC = timezone.utc

# Slug seguro para kubectl set env (mesma regex de _panel_data.py).
_MODEL_SLUG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._:/-]{0,127}$")

# Caminhos dos manifests do monitor (relativos ao repo raiz).
# Usados pelo hotkey [s] para instalar se ausente.
_MANIFEST_PVC = Path(__file__).parent / "manifests" / "56-deile-monitor-pvc.yaml"
_MANIFEST_DEPLOY = Path(__file__).parent / "manifests" / "55-deile-monitor-deployment.yaml"

_PAUSE_DURATIONS = ["30m", "1h", "2h", "custom"]

# Modelos fallback quando ModelsProvider não carregou (sem catálogo YAML).
_MODELS_FALLBACK = (
    "anthropic:claude-opus-4-8",
    "anthropic:claude-sonnet-4-6",
    "anthropic:claude-haiku-4-5",
    "openai:gpt-4",
    "deepseek:deepseek-chat",
    "google:gemini-2.5-pro",
)


# ---------------------------------------------------------------------------
# Dataclasses de estado do monitor
# ---------------------------------------------------------------------------

@dataclass
class MonitorPodInfo:
    """Estado básico do pod deile-monitor, lido via kubectl get pod."""
    found: bool = False
    name: str = ""
    status: str = ""        # Running | Pending | Terminating | …
    ready: bool = False
    age_s: float = 0.0
    restarts: int = 0
    replicas: int = 0       # spec.replicas


@dataclass
class MonitorConfig:
    """Env vars aplicadas no Deployment deile-monitor."""
    tick_interval_s: str = "120"
    preferred_model: str = "(default)"
    notify_user_id: str = "—"
    state_dir: str = "/state"
    pvc_name: str = "deile-monitor-state"
    # DEILE_MONITOR_TICK_INTERVAL_S source extra
    extra: Dict[str, str] = field(default_factory=dict)


@dataclass
class MonitorStateData:
    """Conteúdo do /state/monitor-state.json lido via kubectl exec."""
    raw: Optional[Dict[str, Any]] = None
    last_tick: Optional[int] = None          # CONTADOR de ticks (não é timestamp)
    last_tick_epoch: Optional[float] = None  # epoch da última execução (p/ tempo)
    known_anomalies: Dict[str, Any] = field(default_factory=dict)
    notifications_this_hour: int = 0
    paused_until: Optional[str] = None
    is_paused: bool = False   # /state/monitor-pause existe


@dataclass
class VigiaStatus:
    """Status inferido de uma vigia a partir do audit log."""
    number: int
    name: str
    last_seen_ts: Optional[datetime] = None
    last_line: str = ""
    has_warn: bool = False   # linha continha "FAIL" ou "ERROR" ou "⚠"
    has_action: bool = False  # linha continha "ACTION"


@dataclass
class MonitorSnapshot:
    """Snapshot completo do estado do monitor para renderização."""
    pod: MonitorPodInfo = field(default_factory=MonitorPodInfo)
    config: MonitorConfig = field(default_factory=MonitorConfig)
    state: MonitorStateData = field(default_factory=MonitorStateData)
    vigias: List[VigiaStatus] = field(default_factory=list)
    audit_tail: List[str] = field(default_factory=list)
    notify_tail: List[str] = field(default_factory=list)
    fetch_error: Optional[str] = None


# ---------------------------------------------------------------------------
# Provider de dados do monitor
# ---------------------------------------------------------------------------

class MonitorDataProvider(pd_mod._KubectlProviderMixin):
    """Faz fetch do estado do deile-monitor via kubectl.

    Estratégia de fetch (tolerante a falhas):
    1. kubectl get pod -l app=deile-monitor → MonitorPodInfo
    2. kubectl get deploy/deile-monitor → MonitorConfig (envs)
    3. kubectl exec deploy/deile-monitor -- cat /state/monitor-state.json
    4. kubectl exec ... -- cat /state/monitor-pause (existe?) → is_paused
    5. kubectl exec ... -- tail -15 /state/monitor-audit.log
    6. kubectl exec ... -- tail -5 /state/monitor-notifications.log

    Cada etapa tem fallback independente — se o pod estiver Pending, a
    etapa 3-6 é pulada e snapshot é parcial (mostre o que der).
    """

    _MONITOR_DEPLOY = "deile-monitor"
    _MONITOR_LABEL = "app=deile-monitor"

    def __init__(self, ttl_s: float = 5.0, namespace: str = pd_mod.NS,
                 enabled: bool = True):
        self._kubectl = pd_mod.kubectl_bin()
        self._namespace = namespace
        self._enabled = enabled
        self._cache: pd_mod.Cache[MonitorSnapshot] = pd_mod.Cache(
            ttl_s, self._fetch, fallback=MonitorSnapshot(),
        )

    @property
    def last_error(self) -> Optional[str]:
        return self._cache.last_error

    def get(self, force: bool = False) -> MonitorSnapshot:
        return self._cache.get(force)

    def invalidate(self) -> None:
        self._cache.invalidate()

    def _fetch(self) -> MonitorSnapshot:
        self._check_enabled()
        self._resolve_kubectl()
        if self._kubectl is None:
            raise RuntimeError("kubectl não encontrado")

        snap = MonitorSnapshot()

        # 1. Info do pod
        snap.pod = self._fetch_pod_info()

        # 2. Config do Deployment
        snap.config = self._fetch_config()

        # 3-6. Só se o pod existe e está Running/Pending (exec requer Running)
        if snap.pod.found and snap.pod.status == "Running":
            snap.state = self._fetch_state()
            snap.audit_tail = self._fetch_file_tail("/state/monitor-audit.log", 15)
            snap.notify_tail = self._fetch_file_tail("/state/monitor-notifications.log", 5)
            snap.vigias = self._parse_vigias(snap.audit_tail)

        return snap

    def _fetch_pod_info(self) -> MonitorPodInfo:
        data = pd_mod._capture_json(
            [self._kubectl, "-n", self._namespace, "get", "pods",
             "-l", self._MONITOR_LABEL, "-o", "json"],
            timeout=4.0,
        )
        if not data:
            return MonitorPodInfo(found=False)
        items = data.get("items", [])
        if not items:
            return MonitorPodInfo(found=False)

        # Pega o primeiro pod (Recreate strategy — máximo 1).
        item = items[0]
        meta = item.get("metadata", {})
        status_obj = item.get("status", {})
        phase = status_obj.get("phase", "Unknown")
        cs = status_obj.get("containerStatuses", []) or []
        ready = all(c.get("ready", False) for c in cs) if cs else False
        restarts = sum(c.get("restartCount", 0) for c in cs)
        started_at = pd_mod._parse_k8s_ts(status_obj.get("startTime"))
        now = datetime.now(_UTC)
        age_s = (now - started_at).total_seconds() if started_at else 0.0

        # Réplicas do spec do Deployment (não do pod diretamente — será
        # sobrescrito por _fetch_config, mas serve como fallback).
        return MonitorPodInfo(
            found=True,
            name=meta.get("name", "?"),
            status=phase,
            ready=ready,
            age_s=age_s,
            restarts=restarts,
            replicas=1,
        )

    def _fetch_config(self) -> MonitorConfig:
        data = pd_mod._capture_json(
            [self._kubectl, "-n", self._namespace, "get",
             f"deploy/{self._MONITOR_DEPLOY}", "-o", "json"],
            timeout=4.0,
        )
        cfg = MonitorConfig()
        if not data:
            return cfg

        # replicas
        spec = data.get("spec", {})
        cfg.extra["replicas"] = str(spec.get("replicas", 0))

        containers = (spec.get("template", {}).get("spec", {})
                      .get("containers", []))
        envs: Dict[str, str] = {}
        for c in containers:
            for ev in c.get("env", []):
                n = ev.get("name", "")
                v = ev.get("value", "")
                if n:
                    envs[n] = v

        cfg.tick_interval_s = envs.get("DEILE_MONITOR_TICK_INTERVAL_S", "120")
        preferred = envs.get("DEILE_PREFERRED_MODEL", "")
        cfg.preferred_model = preferred if preferred else "(default)"
        cfg.state_dir = envs.get("DEILE_MONITOR_STATE_DIR", "/state")

        # Volumes: encontrar PVC name
        vols = spec.get("template", {}).get("spec", {}).get("volumes", [])
        for v in vols:
            pvc = v.get("persistentVolumeClaim", {})
            if pvc:
                cfg.pvc_name = pvc.get("claimName", "deile-monitor-state")
                break

        return cfg

    def _fetch_state(self) -> MonitorStateData:
        state_json = self._exec_cat("/state/monitor-state.json")
        pause_exists = self._exec_test_file("/state/monitor-pause")
        notify_uid = self._exec_cat("/state/notify-user-id")

        st = MonitorStateData(is_paused=pause_exists)
        if notify_uid:
            # Guarda em atributo extra acessível pelo render.
            st.raw = {"_notify_user_id": notify_uid.strip()}
        if not state_json:
            return st
        try:
            data = json.loads(state_json)
        except (json.JSONDecodeError, ValueError):
            return st
        st.raw = data
        st.last_tick = data.get("last_tick")
        st.last_tick_epoch = data.get("last_tick_epoch")
        st.known_anomalies = data.get("known_anomalies") or {}
        st.notifications_this_hour = int(data.get("notifications_this_hour", 0))
        st.paused_until = data.get("paused_until")
        return st

    def _exec_cat(self, path: str) -> Optional[str]:
        """Lê arquivo no pod via kubectl exec. Falha silenciosa → None."""
        if self._kubectl is None:
            return None
        out = pd_mod._capture_text(
            [self._kubectl, "-n", self._namespace, "exec",
             f"deploy/{self._MONITOR_DEPLOY}", "--",
             "cat", path],
            timeout=5.0,
        )
        return out

    def _exec_test_file(self, path: str) -> bool:
        """Testa existência de arquivo via kubectl exec. Falha → False."""
        if self._kubectl is None:
            return False
        try:
            r = subprocess.run(
                [self._kubectl, "-n", self._namespace, "exec",
                 f"deploy/{self._MONITOR_DEPLOY}", "--",
                 "test", "-f", path],
                capture_output=True, timeout=4.0,
            )
            return r.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _fetch_file_tail(self, path: str, n: int) -> List[str]:
        """tail -N de arquivo no pod via kubectl exec."""
        raw = pd_mod._capture_text(
            [self._kubectl, "-n", self._namespace, "exec",
             f"deploy/{self._MONITOR_DEPLOY}", "--",
             "tail", f"-{n}", path],
            timeout=5.0,
        )
        if not raw:
            return []
        return [line for line in raw.splitlines() if line.strip()]

    def _parse_vigias(self, audit_lines: List[str]) -> List[VigiaStatus]:
        """Infere status de V1-V7 a partir das últimas linhas do audit log.

        Heurística best-effort: a persona não loga em formato canônico
        V<n> de forma garantida (gap documentado — ver relatório final).
        Procuramos "V1" ... "V7" no texto da linha e classificamos.
        """
        # Status inicial: todas desconhecidas (sem dado no log).
        vigias: Dict[int, VigiaStatus] = {
            n: VigiaStatus(number=n, name=_VIGIA_NAMES[n])
            for n in range(1, 8)
        }
        for raw_line in audit_lines:
            m = _AUDIT_TS_RE.match(raw_line)
            ts_str = m.group(1) if m else ""
            body = m.group(2) if m else raw_line
            ts: Optional[datetime] = None
            if ts_str:
                ts = pd_mod._parse_k8s_ts(ts_str)
            for vm in _VIGIA_RE.finditer(body):
                vn = int(vm.group(1))
                if vn not in vigias:
                    continue
                v = vigias[vn]
                # Atualiza com linha mais recente (audit está em ordem cronológica).
                if ts is not None:
                    v.last_seen_ts = ts
                v.last_line = body[:80]
                upper = body.upper()
                v.has_warn = any(kw in upper for kw in ("FAIL", "ERROR", "⚠", "URGENTE"))
                v.has_action = "ACTION" in upper
        return list(vigias.values())


# ---------------------------------------------------------------------------
# View principal
# ---------------------------------------------------------------------------

class MonitorView:
    """Tela cheia dedicada ao deile-monitor (hotkey [M] MAIÚSCULO).

    Implementa o contrato View de _panel.py (render/handle_key/intercepts_key
    /on_mount/on_unmount) sem herdar diretamente de View para evitar
    import circular — _panel.py importa esta classe com lazy import.

    Sensibilidade ao case:
    - [M] MAIÚSCULO → esta view (DashboardView.handle_key mapeia "M").
    - [m] minúsculo → ModelSwitcherView (já existia antes).
    O KeyReader retorna a letra exatamente como veio do terminal,
    então a distinção de case é natural.
    """

    name = "monitor-view"
    title = "DEILE-Monitor · Supervisor de Cluster"
    refresh_s = 3.0

    # Hotkeys locais (dentro da view).
    # [t] FORÇA TICK — conflito com PodWatchView.handle_key onde "t" é
    # "tmp resize". Não há conflito aqui porque MonitorView é uma view
    # separada com handler próprio.
    HOTKEYS_BASE = (
        "[t]força-tick  [p]pause  [r]resume  [a]ck fingerprint  "
        "[i]interval  [u]user-id  [m]model  "
        "[k]stop (scale 0)  [s]start/install  [l]audit-log tail  "
        "[esc]/[q]volta"
    )

    def __init__(self, data=None, monitor_provider: Optional[MonitorDataProvider] = None):
        # `data` é o PanelData completo (pode ser None em demo).
        self.data = data
        # Provider dedicado; criado aqui se não injetado (facilita testes).
        if monitor_provider is not None:
            self._mon = monitor_provider
        elif data is not None and hasattr(data, "context"):
            ns = data.context.namespace
            enabled = data.context.k8s_available
            self._mon = MonitorDataProvider(namespace=ns, enabled=enabled)
        else:
            self._mon = MonitorDataProvider(enabled=False)

        # Estado de UI da view.
        self._mode: Optional[str] = None  # None | "pause-menu" | "ack-input"
        #   | "interval-input" | "user-id-input" | "model-picker"
        #   | "confirm-stop" | "confirm-start" | "log-tail"
        self._input_buf: str = ""
        self._pause_cursor: int = 0
        self._model_cursor: int = 0
        self._model_options: List[str] = []
        self._last_msg: str = ""
        self._last_ok: bool = True
        self._log_tail_lines: List[str] = []
        self._log_tail_stop = threading.Event()
        self._log_tail_thread: Optional[threading.Thread] = None

    # ------------------------------------------------------------------
    # View protocol
    # ------------------------------------------------------------------

    def on_mount(self, app) -> None:
        self._mon.invalidate()

    def on_unmount(self, app) -> None:
        self._stop_log_tail()
        self._mode = None
        self._input_buf = ""
        self._last_msg = ""

    def intercepts_key(self, key: str) -> bool:
        """Captura ESC enquanto há modal aberto, para fechar o modal
        sem fazer pop da view (comportamento análogo ao DispatchMatrixView)."""
        return self._mode is not None and key == "ESC"

    # ------------------------------------------------------------------
    # Rendering principal
    # ------------------------------------------------------------------

    def render(self, app) -> RenderableType:
        try:
            return self._render_safe(app)
        except Exception as exc:  # noqa: BLE001 — view nunca derruba o loop
            import traceback as _tb
            tb = "".join(_tb.format_exception_only(type(exc), exc)).strip()
            return Group(
                Text("MonitorView: render falhou", style="bold red"),
                Panel(Text(tb, style="red"), title="ERRO", border_style="red"),
                Text(self.HOTKEYS_BASE, style="dim"),
            )

    def _render_safe(self, app) -> RenderableType:
        from _panel import _head_panel, _footer_panel  # noqa: PLC0415 — lazy

        snap = self._mon.get()
        ns = (self.data.context.namespace
              if self.data is not None and hasattr(self.data, "context")
              else "—")

        sections: List[RenderableType] = [
            _head_panel(f"{self.title} · {ns}", app),
        ]

        # --- Estado especial: monitor não instalado ---
        if not snap.pod.found:
            sections.append(Panel(
                Text(
                    "deile-monitor NÃO INSTALADO (ou pod não encontrado).\n\n"
                    "  Pressione [s] para aplicar os manifests e subir o monitor.\n"
                    "  Pressione [esc] para voltar.",
                    style="bold yellow",
                ),
                title="[bold yellow]MONITOR NÃO INSTALADO[/bold yellow]",
                title_align="left", border_style="yellow",
            ))
            sections.append(Panel(
                Text(self.HOTKEYS_BASE, style="dim"),
                border_style="dim", box=box.SIMPLE,
            ))
            return Group(*sections)

        # --- Estado especial: pod Pending ---
        if snap.pod.status == "Pending":
            sections.append(Panel(
                Text(
                    f"pod: {snap.pod.name}\n"
                    f"status: Pending — aguardando ready (schedule / PVC attach).\n\n"
                    "  [esc] para voltar   [k] para parar",
                    style="bold yellow",
                ),
                title="[bold yellow]MONITOR INICIANDO[/bold yellow]",
                title_align="left", border_style="yellow",
            ))
            sections.append(Panel(
                Text(self.HOTKEYS_BASE, style="dim"),
                border_style="dim", box=box.SIMPLE,
            ))
            return Group(*sections)

        # --- Cabeçalho do pod ---
        sections.append(self._render_pod_header(snap))

        # --- Modo log-tail full-screen ---
        if self._mode == "log-tail":
            sections.append(self._render_log_tail())
            sections.append(Panel(
                Text("[q]/[esc] sair do tail", style="dim"),
                border_style="dim", box=box.SIMPLE,
            ))
            return Group(*sections)

        # --- Layout principal ---
        sections.append(self._render_config_vigias(snap))
        sections.append(self._render_anomalias(snap))
        sections.append(self._render_last_tick(snap))
        sections.append(self._render_audit_notify(snap))

        # --- Modal (se aberto) ---
        if self._mode == "pause-menu":
            sections.append(self._render_pause_menu())
        elif self._mode == "ack-input":
            sections.append(self._render_input_prompt(
                "Fingerprint da anomalia para ack (24h de supressão):",
            ))
        elif self._mode == "interval-input":
            sections.append(self._render_input_prompt(
                "Novo intervalo de tick em segundos (30–3600):",
            ))
        elif self._mode == "user-id-input":
            sections.append(self._render_input_prompt(
                "Discord snowflake (user ID) para notificações:",
            ))
        elif self._mode == "model-picker":
            sections.append(self._render_model_picker())
        elif self._mode == "confirm-stop":
            sections.append(self._render_confirm(
                "Parar o monitor? (scale to 0 — dados do PVC preservados)",
                "s=confirmar  n=cancelar",
            ))
        elif self._mode == "confirm-start":
            sections.append(self._render_confirm(
                "Subir o monitor? (scale=1 ou aplicar manifests se ausente)",
                "s=confirmar  n=cancelar",
            ))

        # --- Feedback da última ação ---
        if self._last_msg:
            border = "green" if self._last_ok else "red"
            style = "bold green" if self._last_ok else "bold red"
            sections.append(Panel(
                Text(self._last_msg, style=style),
                title="ÚLTIMA AÇÃO",
                title_align="left",
                border_style=border,
            ))

        # Footer PINADO via Layout — não como última seção de um Group, que
        # o ``Live(screen=True)`` cortaria quando o conteúdo passa da altura
        # do terminal (era por isso que a barra de atalhos sumia). head fixo
        # no topo, footer fixo embaixo, body no meio (clipa se transbordar) —
        # mesmo padrão da DashboardView principal.
        footer = Panel(
            Text(self.HOTKEYS_BASE, style="dim"),
            border_style="dim", box=box.SIMPLE,
        )
        body: RenderableType = (
            Group(*sections[1:]) if len(sections) > 1 else Text(""))
        layout = Layout()
        layout.split_column(
            Layout(sections[0], name="head", size=4),
            Layout(body, name="body", ratio=1),
            Layout(footer, name="footer", size=3),
        )
        return layout

    # --- Blocos de render ---

    def _render_pod_header(self, snap: MonitorSnapshot) -> RenderableType:
        p = snap.pod
        cfg = snap.config
        st = snap.state
        now = datetime.now(_UTC)

        age_str = _fmt_age_s(p.age_s)
        status_style = "bold green" if p.status == "Running" else "bold yellow"
        ready_icon = "✓" if p.ready else "✗"

        # Calcula próximo tick: last_tick_epoch + interval_s. O state grava
        # ``last_tick`` como CONTADOR (int) e ``last_tick_epoch`` como o epoch
        # da última execução — é este último que serve para o cálculo de tempo.
        next_tick_str = "—"
        try:
            interval_s = int(cfg.tick_interval_s)
            if st.last_tick_epoch:
                elapsed = now.timestamp() - float(st.last_tick_epoch)
                remaining = max(0, interval_s - elapsed)
                next_tick_str = f"{int(remaining)}s"
        except (ValueError, TypeError):
            pass

        paused_label = "sim" if st.is_paused else "não"
        notif_1h = str(st.notifications_this_hour)

        lines = [
            Text.assemble(
                ("pod: ", "dim"), (p.name, "bold"),
                ("   status: ", "dim"),
                (p.status, status_style),
                (f" {ready_icon}", ""),
                ("   age: ", "dim"), (age_str, "bold"),
                ("   restarts: ", "dim"), (str(p.restarts), "bold"),
            ),
            Text.assemble(
                ("próximo tick em: ", "dim"),
                (next_tick_str, "bold cyan"),
                ("   notificações/h: ", "dim"),
                (notif_1h, "bold"),
                ("   pausado: ", "dim"),
                (paused_label, "bold yellow" if st.is_paused else "bold green"),
            ),
        ]
        return Panel(
            Group(*lines),
            title=f"[bold]DEILE-MONITOR · {snap.pod.name}[/bold]",
            title_align="left",
            border_style="cyan",
        )

    def _render_config_vigias(self, snap: MonitorSnapshot) -> RenderableType:
        """Bloco CONFIG (esquerda) + VIGIAS (direita) lado a lado."""
        # CONFIG
        cfg = snap.config
        # Tenta ler notify-user-id do state.raw se carregado.
        uid = "—"
        if snap.state.raw and isinstance(snap.state.raw, dict):
            uid = snap.state.raw.get("_notify_user_id") or "—"

        cfg_lines = [
            Text.assemble(("Model:      ", "dim"),
                          (cfg.preferred_model, "bold cyan")),
            Text.assemble(("Tick:       ", "dim"),
                          (f"{cfg.tick_interval_s}s", "bold")),
            Text.assemble(("Flood cap:  ", "dim"),
                          ("8/hora (padrão)", "dim")),
            Text.assemble(("NotifyUID:  ", "dim"),
                          (uid[:24], "bold")),
            Text.assemble(("State PVC:  ", "dim"),
                          (f"{cfg.pvc_name} (256Mi)", "dim")),
        ]
        config_panel = Panel(
            Group(*cfg_lines),
            title="[bold]CONFIG[/bold]",
            title_align="left",
            border_style="blue",
        )

        # VIGIAS
        tbl = Table(box=box.SIMPLE, show_header=False, expand=True, pad_edge=False)
        tbl.add_column("v", style="bold", no_wrap=True)
        tbl.add_column("nome")
        tbl.add_column("status", no_wrap=True)
        tbl.add_column("detalhe")

        for v in snap.vigias:
            if v.has_warn:
                icon = "✗"
                icon_style = "bold red"
            elif v.last_seen_ts:
                icon = "✓"
                icon_style = "bold green"
            else:
                icon = "?"
                icon_style = "dim"

            ts_str = ""
            if v.last_seen_ts:
                age_s = (datetime.now(_UTC) - v.last_seen_ts).total_seconds()
                ts_str = _fmt_age_s(age_s) + " ago"

            tbl.add_row(
                Text(f"V{v.number}", style="bold cyan"),
                v.name,
                Text(icon, style=icon_style),
                Text(ts_str, style="dim"),
            )

        vigias_panel = Panel(
            tbl,
            title="[bold]VIGIAS (último tick)[/bold]",
            title_align="left",
            border_style="blue",
        )

        # Retorna Group para renderização sequencial (layout side-by-side
        # via rich.Layout exigiria altura fixa — evitamos para manter a
        # UI adaptativa como manda o princípio 15).
        return Group(config_panel, vigias_panel)

    def _render_anomalias(self, snap: MonitorSnapshot) -> RenderableType:
        anomalias = snap.state.known_anomalies
        if not anomalias:
            body: RenderableType = Text("· sem anomalias ativas", style="dim green")
        else:
            tbl = Table(box=box.SIMPLE, show_header=True, expand=True, pad_edge=False)
            tbl.add_column("fingerprint", style="bold")
            tbl.add_column("first_seen")
            tbl.add_column("count", justify="right")
            tbl.add_column("last_notified")
            for fp, info in list(anomalias.items())[:10]:
                if not isinstance(info, dict):
                    continue
                tbl.add_row(
                    fp,
                    str(info.get("first_seen", "—"))[:16],
                    str(info.get("count", "?")),
                    str(info.get("last_notified", "—"))[:16],
                )
            body = tbl
        return Panel(
            body,
            title="[bold]ANOMALIAS ATIVAS[/bold]",
            title_align="left",
            border_style="yellow",
        )

    def _render_last_tick(self, snap: MonitorSnapshot) -> RenderableType:
        st = snap.state
        if st.last_tick_epoch:
            when = datetime.fromtimestamp(
                float(st.last_tick_epoch), _UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
            label = f"{when}   (tick #{st.last_tick})" if st.last_tick else when
            txt = Text(label, style="bold")
        elif st.last_tick is not None:
            txt = Text(f"tick #{st.last_tick}", style="bold")
        else:
            txt = Text("· sem tick registrado", style="dim")
        return Panel(txt, title="[bold]ÚLTIMO TICK[/bold]",
                     title_align="left", border_style="dim")

    def _render_audit_notify(self, snap: MonitorSnapshot) -> RenderableType:
        audit_lines = snap.audit_tail or ["· log vazio ou não acessível"]
        audit_body = Text("\n".join(audit_lines[-15:]), style="dim")
        audit_panel = Panel(
            audit_body,
            title="[bold]AUDIT LOG (últimas 15)[/bold]",
            title_align="left", border_style="dim",
        )
        notify_lines = snap.notify_tail or ["· sem notificações recentes"]
        notify_body = Text("\n".join(notify_lines[-5:]), style="dim")
        notify_panel = Panel(
            notify_body,
            title="[bold]NOTIFICAÇÕES (últimas 5)[/bold]",
            title_align="left", border_style="dim",
        )
        return Group(audit_panel, notify_panel)

    # --- Modais ---

    def _render_pause_menu(self) -> RenderableType:
        lines = []
        for i, dur in enumerate(_PAUSE_DURATIONS):
            prefix = "▶ " if i == self._pause_cursor else "  "
            style = "bold cyan reverse" if i == self._pause_cursor else ""
            lines.append(Text(f"{prefix}[{i+1}] {dur}", style=style))
        return Panel(
            Group(*lines),
            title="[bold]PAUSA — escolha duração[/bold]",
            title_align="left",
            border_style="yellow",
        )

    def _render_input_prompt(self, prompt: str) -> RenderableType:
        return Panel(
            Group(
                Text(prompt, style="dim"),
                Text(f"> {self._input_buf}_", style="bold cyan"),
            ),
            title="[bold]INPUT[/bold]",
            title_align="left",
            border_style="cyan",
        )

    def _render_model_picker(self) -> RenderableType:
        lines = []
        for i, slug in enumerate(self._model_options):
            prefix = "▶ " if i == self._model_cursor else "  "
            style = "bold cyan reverse" if i == self._model_cursor else ""
            lines.append(Text(f"{prefix}{slug}", style=style))
        return Panel(
            Group(*lines[:10]),
            title="[bold]MODEL — escolha o slug[/bold]",
            title_align="left",
            border_style="cyan",
        )

    def _render_confirm(self, msg: str, keys: str) -> RenderableType:
        return Panel(
            Group(
                Text(msg, style="bold yellow"),
                Text(keys, style="dim"),
            ),
            title="[bold yellow]CONFIRMAR[/bold yellow]",
            title_align="left",
            border_style="yellow",
        )

    def _render_log_tail(self) -> RenderableType:
        lines = list(self._log_tail_lines[-40:])
        body = Text("\n".join(lines) if lines else "· aguardando log...", style="dim")
        return Panel(
            body,
            title="[bold]AUDIT LOG — tail (live)[/bold]",
            title_align="left",
            border_style="dim",
        )

    # ------------------------------------------------------------------
    # Key handler
    # ------------------------------------------------------------------

    def handle_key(self, key: str, app) -> Any:
        from _panel import ActionResult  # noqa: PLC0415 — lazy

        # ESC interceptado (modal aberto) → fecha modal.
        if key == "ESC" and self._mode is not None:
            self._mode = None
            self._input_buf = ""
            return ActionResult.refresh()

        if self._mode == "pause-menu":
            return self._handle_pause_menu_key(key, app)
        if self._mode == "ack-input":
            return self._handle_input_key(key, app, self._apply_ack)
        if self._mode == "interval-input":
            return self._handle_input_key(key, app, self._apply_interval)
        if self._mode == "user-id-input":
            return self._handle_input_key(key, app, self._apply_user_id)
        if self._mode == "model-picker":
            return self._handle_model_picker_key(key, app)
        if self._mode == "confirm-stop":
            return self._handle_confirm_key(key, app, self._apply_stop)
        if self._mode == "confirm-start":
            return self._handle_confirm_key(key, app, self._apply_start)
        if self._mode == "log-tail":
            if key in ("ESC", "q"):
                self._stop_log_tail()
                self._mode = None
                return ActionResult.refresh()
            return ActionResult()

        # --- Modo normal (browsing) ---
        if key == "q" or key == "ESC":
            return ActionResult.back()

        if key == "t":
            return self._apply_force_tick(app)
        if key == "p":
            self._mode = "pause-menu"
            self._pause_cursor = 0
            return ActionResult.refresh()
        if key == "r":
            return self._apply_resume(app)
        if key == "a":
            self._mode = "ack-input"
            self._input_buf = ""
            return ActionResult.refresh()
        if key == "i":
            self._mode = "interval-input"
            self._input_buf = ""
            return ActionResult.refresh()
        if key == "u":
            self._mode = "user-id-input"
            self._input_buf = ""
            return ActionResult.refresh()
        if key == "m":
            self._open_model_picker()
            return ActionResult.refresh()
        if key == "k":
            self._mode = "confirm-stop"
            return ActionResult.refresh()
        if key == "s":
            self._mode = "confirm-start"
            return ActionResult.refresh()
        if key == "l":
            self._start_log_tail()
            self._mode = "log-tail"
            return ActionResult.refresh()

        return ActionResult()

    # --- Handlers de modo ---

    def _handle_pause_menu_key(self, key: str, app) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        if key == "UP" or key == "k":
            self._pause_cursor = max(0, self._pause_cursor - 1)
            return ActionResult.refresh()
        if key == "DOWN" or key == "j":
            self._pause_cursor = min(len(_PAUSE_DURATIONS) - 1,
                                     self._pause_cursor + 1)
            return ActionResult.refresh()
        if key in ("1", "2", "3", "4"):
            idx = int(key) - 1
            if 0 <= idx < len(_PAUSE_DURATIONS):
                self._pause_cursor = idx
        if key == "\r" or key == "\n" or key in ("1", "2", "3", "4"):
            dur = _PAUSE_DURATIONS[self._pause_cursor]
            if dur == "custom":
                self._mode = "interval-input"
                self._input_buf = ""
                return ActionResult.refresh()
            self._apply_pause(dur, app)
            self._mode = None
            return ActionResult.refresh()
        if key == "ESC":
            self._mode = None
            return ActionResult.refresh()
        return ActionResult()

    def _handle_input_key(self, key: str, app, apply_fn) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        if key == "ESC":
            self._mode = None
            self._input_buf = ""
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            val = self._input_buf.strip()
            self._input_buf = ""
            self._mode = None
            if val:
                apply_fn(val, app)
            return ActionResult.refresh()
        if key == "BACKSPACE" or key == "\x7f":
            self._input_buf = self._input_buf[:-1]
            return ActionResult.refresh()
        # Aceita caracteres imprimíveis ASCII.
        if len(key) == 1 and 0x20 <= ord(key) <= 0x7E:
            self._input_buf += key
            return ActionResult.refresh()
        return ActionResult()

    def _handle_model_picker_key(self, key: str, app) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        if key == "ESC":
            self._mode = None
            return ActionResult.refresh()
        if key in ("UP", "k"):
            self._model_cursor = max(0, self._model_cursor - 1)
            return ActionResult.refresh()
        if key in ("DOWN", "j"):
            self._model_cursor = min(len(self._model_options) - 1,
                                     self._model_cursor + 1)
            return ActionResult.refresh()
        if key in ("\r", "\n"):
            if self._model_options and 0 <= self._model_cursor < len(self._model_options):
                slug = self._model_options[self._model_cursor]
                self._mode = None
                self._apply_model(slug, app)
            return ActionResult.refresh()
        return ActionResult()

    def _handle_confirm_key(self, key: str, app, action_fn) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        if key in ("s", "y"):
            self._mode = None
            action_fn(app)
            return ActionResult.refresh()
        self._mode = None
        self._last_msg = "Ação cancelada."
        self._last_ok = True
        return ActionResult.refresh()

    # --- Ações kubectl ---

    def _kubectl_cmd(self) -> Optional[str]:
        return pd_mod.kubectl_bin()

    def _ns(self) -> str:
        if self.data is not None and hasattr(self.data, "context"):
            return self.data.context.namespace
        return pd_mod.NS

    def _exec(self, args: List[str], timeout: float = 8.0) -> Tuple[bool, str]:
        """Executa comando kubectl e retorna (sucesso, stdout+stderr)."""
        kubectl = self._kubectl_cmd()
        if kubectl is None:
            return False, "kubectl não encontrado"
        try:
            r = subprocess.run(
                [kubectl] + args,
                capture_output=True, text=True, timeout=timeout,
            )
            out = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, out.strip()
        except subprocess.TimeoutExpired:
            return False, "timeout ao executar kubectl"
        except OSError as exc:
            return False, str(exc)

    def _apply_force_tick(self, app) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        ok, out = self._exec([
            "-n", self._ns(), "exec",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--", "rm", "-f", "/state/monitor-state.json",
        ])
        if ok:
            self._last_msg = "Tick forçado: monitor-state.json removido → próximo tick imediato."
        else:
            self._last_msg = f"Falha ao forçar tick: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()
        return ActionResult.refresh()

    def _apply_pause(self, duration: str, app) -> None:
        """Cria /state/monitor-pause. Para duração custom, usa 'p' de input."""
        ok, out = self._exec([
            "-n", self._ns(), "exec",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--", "touch", "/state/monitor-pause",
        ])
        if ok:
            self._last_msg = f"Monitor pausado por {duration} (kill-switch ativo)."
        else:
            self._last_msg = f"Falha ao pausar: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _apply_resume(self, app) -> Any:
        from _panel import ActionResult  # noqa: PLC0415

        ok, out = self._exec([
            "-n", self._ns(), "exec",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--", "rm", "-f", "/state/monitor-pause",
        ])
        if ok:
            self._last_msg = "Monitor resumido: monitor-pause removido."
        else:
            self._last_msg = f"Falha ao resumir: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()
        return ActionResult.refresh()

    def _apply_ack(self, fingerprint: str, app) -> None:
        # Escreve via exec sh -c 'python3 -c ...' para manipular o JSON
        # sem depender de jq no container.
        script = (
            "import json,sys,time;"
            "p='/state/monitor-state.json';"
            "d=json.loads(open(p).read()) if __import__('os').path.exists(p) else {};"
            "d.setdefault('known_anomalies',{}).setdefault"
            f"('{fingerprint}',{{}})['acked_until']="
            "time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime(time.time()+86400));"
            "open(p,'w').write(json.dumps(d))"
        )
        ok, out = self._exec([
            "-n", self._ns(), "exec",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--", "python3", "-c", script,
        ])
        if ok:
            self._last_msg = f"Ack registrado: {fingerprint} suprimido por 24h."
        else:
            self._last_msg = f"Falha ao registrar ack: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _apply_interval(self, val: str, app) -> None:
        try:
            n = int(val)
        except ValueError:
            self._last_msg = f"Valor inválido: {val!r} (esperado inteiro 30–3600)"
            self._last_ok = False
            return
        if not 30 <= n <= 3600:
            self._last_msg = f"Intervalo fora do range: {n} (30–3600)"
            self._last_ok = False
            return
        ok, out = self._exec([
            "-n", self._ns(), "set", "env",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            f"DEILE_MONITOR_TICK_INTERVAL_S={n}",
        ])
        if ok:
            self._last_msg = f"Tick interval atualizado para {n}s (próximo restart do pod aplica)."
        else:
            self._last_msg = f"Falha ao setar interval: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _apply_user_id(self, uid: str, app) -> None:
        # Escreve o UID em /state/notify-user-id via exec sh -c.
        ok, out = self._exec([
            "-n", self._ns(), "exec",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--", "sh", "-c", f"echo '{uid}' > /state/notify-user-id",
        ])
        if ok:
            self._last_msg = f"notify-user-id atualizado: {uid}"
        else:
            self._last_msg = f"Falha ao setar user-id: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _open_model_picker(self) -> None:
        models: List[str] = []
        if self.data is not None and hasattr(self.data, "models"):
            try:
                for m in self.data.models.get():
                    slug = getattr(m, "slug", None)
                    if isinstance(slug, str) and slug:
                        models.append(slug)
            except Exception:  # noqa: BLE001
                pass
        if not models:
            models = list(_MODELS_FALLBACK)
        self._model_options = ["(limpar override)", *models]
        self._model_cursor = 0
        self._mode = "model-picker"

    def _apply_model(self, slug: str, app) -> None:
        if slug == "(limpar override)":
            ok, out = self._exec([
                "-n", self._ns(), "set", "env",
                f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
                "DEILE_PREFERRED_MODEL-",
            ])
            if ok:
                self._last_msg = "DEILE_PREFERRED_MODEL removido (default do cluster)."
            else:
                self._last_msg = f"Falha ao limpar model: {out[:120]}"
        else:
            if not _MODEL_SLUG_RE.match(slug):
                self._last_msg = f"Slug inválido: {slug!r}"
                self._last_ok = False
                return
            ok, out = self._exec([
                "-n", self._ns(), "set", "env",
                f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
                f"DEILE_PREFERRED_MODEL={slug}",
            ])
            if ok:
                self._last_msg = f"Model atualizado: {slug}"
            else:
                self._last_msg = f"Falha ao setar model: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _apply_stop(self, app) -> None:
        ok, out = self._exec([
            "-n", self._ns(), "scale",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--replicas=0",
        ])
        if ok:
            self._last_msg = "Monitor parado (scale=0). Dados do PVC preservados."
        else:
            self._last_msg = f"Falha ao parar: {out[:120]}"
        self._last_ok = ok
        self._mon.invalidate()

    def _apply_start(self, app) -> None:
        # Tenta scale=1 primeiro; se o Deployment não existir, aplica manifests.
        ok, out = self._exec([
            "-n", self._ns(), "scale",
            f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
            "--replicas=1",
        ])
        if ok:
            self._last_msg = "Monitor iniciado (scale=1)."
            self._last_ok = True
            self._mon.invalidate()
            return
        # Deployment ausente → apply manifests.
        pvc_path = str(_MANIFEST_PVC)
        dep_path = str(_MANIFEST_DEPLOY)
        ok_pvc, out_pvc = self._exec(
            ["-n", self._ns(), "apply", "-f", pvc_path], timeout=20.0
        )
        ok_dep, out_dep = self._exec(
            ["-n", self._ns(), "apply", "-f", dep_path], timeout=20.0
        )
        if ok_pvc and ok_dep:
            self._last_msg = "Manifests aplicados. Monitor iniciando..."
        else:
            msgs = []
            if not ok_pvc:
                msgs.append(f"PVC: {out_pvc[:80]}")
            if not ok_dep:
                msgs.append(f"Deploy: {out_dep[:80]}")
            self._last_msg = "Falha parcial ao instalar: " + " | ".join(msgs)
        self._last_ok = ok_pvc and ok_dep
        self._mon.invalidate()

    # --- Log tail live ---

    def _start_log_tail(self) -> None:
        self._stop_log_tail()
        self._log_tail_lines = []
        self._log_tail_stop.clear()
        self._log_tail_thread = threading.Thread(
            target=self._log_tail_loop, daemon=True, name="monitor-log-tail",
        )
        self._log_tail_thread.start()

    def _stop_log_tail(self) -> None:
        self._log_tail_stop.set()
        if self._log_tail_thread is not None:
            self._log_tail_thread.join(timeout=2.0)
            self._log_tail_thread = None

    def _log_tail_loop(self) -> None:
        """Faz fetch periódico do audit log (pull a cada 2s) em background."""
        kubectl = pd_mod.kubectl_bin()
        if kubectl is None:
            self._log_tail_lines = ["kubectl não encontrado"]
            return
        while not self._log_tail_stop.is_set():
            lines = pd_mod._capture_text(
                [kubectl, "-n", self._ns(), "exec",
                 f"deploy/{MonitorDataProvider._MONITOR_DEPLOY}",
                 "--", "tail", "-200", "/state/monitor-audit.log"],
                timeout=5.0,
            )
            if lines:
                self._log_tail_lines = [l for l in lines.splitlines() if l.strip()]
            self._log_tail_stop.wait(timeout=2.0)


# ---------------------------------------------------------------------------
# Helpers locais
# ---------------------------------------------------------------------------

def _fmt_age_s(seconds: float) -> str:
    """Formata segundos como "1h05m", "47s", "3d"."""
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    if s < 86400:
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m:02d}m" if m else f"{h}h"
    return f"{s // 86400}d"
