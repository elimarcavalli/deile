"""Dados-fantasma para o painel rodar em modo demo (sem cluster).

Usado quando `kubectl` não está disponível ou o cluster está fora — a UI
ainda abre e mostra o esqueleto preenchido, em vez de bater contra uma
fonte morta a cada refresh.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple


@dataclass
class DemoPodRow:
    icon: str
    name: str
    role: str
    status: str
    age: str
    restarts: str
    last_activity: str
    doing_now: str
    busy: bool = False


PODS: List[DemoPodRow] = [
    DemoPodRow("●", "deile-pipeline-7f8d", "pipeline", "Running", "17m", "0",
               "23s ago", "monitor rodando, ocioso"),
    DemoPodRow("●", "deile-worker-abc-1", "worker",   "Running", "2h",  "0",
               "4m ago",  "idle"),
    DemoPodRow("⚡", "deile-worker-abc-2", "worker",   "Running", "2h",  "0",
               "0s ago",  "IMPL #296 [t+240s]", busy=True),
    DemoPodRow("●", "deilebot-xyz",       "bot",      "Running", "5h",  "0",
               "1m ago",  "0 inflight DMs"),
    DemoPodRow("●", "deile-shell-def0",   "shell",    "Running", "5h",  "0",
               "—",       "idle"),
]

PIPELINE: Dict[str, object] = {
    "running_for_human": "1h7m",
    "last_action_age_human": "23s ago",
    "last_action_summary": "mention PR291: triggers=reviewer",
    "dispatches_24h": 14,
    "mentions_24h": 5,
}

ISSUE_STATES = {"nova": 2, "em_refinamento": 1, "em_arquitetura": 1,
                "em_implementacao": 3, "em_pr": 0, "bloqueada": 1,
                "aguardando_stakeholder": 0}
PR_STATES = {"pendente": 1, "em_andamento": 1, "concluida": 0}

ACTIVITY: List[Tuple[str, str, str, str, str]] = [
    ("14:51:48", "worker-2", "start  implement", "#296",   "attempt 2  budget 0s"),
    ("14:51:30", "pipeline", "claim",            "#294",   "batch:7a2c → analyst"),
    ("14:50:55", "worker-1", "done   review",    "#293",   "veredito APROVADO"),
    ("14:50:30", "pipeline", "merge",            "PR#293", "commit 6bba656 (green suite)"),
    ("14:49:12", "notifier", "→discord",         "",       "PR #293 merged"),
    ("14:48:30", "pipeline", "resume",           "#281",   "attempt 3/5  backoff 4×"),
    ("14:47:30", "pipeline", "classify",         "PR#293", "→ ~review:pendente"),
    ("14:46:08", "worker-1", "start  review",    "PR#293", "budget 0s"),
]

ALERTS: List[Tuple[str, str]] = [
    ("⚠", "#281 attempt 3/5 — próximo loop com mesmo erro vai bloquear"),
]

TOKENS = {
    "providers": [("anthropic", 9.43), ("openai", 1.12), ("deepseek", 0.85)],
    "total_24h": 11.40,
    "total_1h": 1.32,
    "records_24h": 187,
}

DECISIONS: List[Tuple[str, str]] = [
    ("#296",   "claim+dispatch implement"),
    ("#294",   "refine round 2 (em_refinamento)"),
    ("#281",   "blocked (escalou TIMEOUT 2×)"),
]
