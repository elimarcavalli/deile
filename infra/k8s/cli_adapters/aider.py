#!/usr/bin/env python3
"""cli_adapters.aider — adapter do Aider CLI (frota multi-worker, Tier 1).

Único worker com ``git_strategy="cli_autocommit"``: o Aider commita
(``--auto-commits``); o wrapper só faz push + gate de teste. Headless via
``aider --message-file <brief> --yes-always``. Maquinaria genérica (lease,
heartbeat, subprocess, gate de git, HTTP) vem do ``cli_worker_server`` +
``_worker_core``.

Decisões não-óbvias:

* **Atribuição (regra do projeto):** ``--no-attribute-author``,
  ``--no-attribute-committer`` e ``--no-attribute-commit-message-author`` —
  NÃO prefixar commits com "aider"/Co-Authored-By.
* **Single-pass gotcha:** ``--message-file --yes-always`` é uma passada só →
  pode commitar código quebrado. O gate pós-run de teste (server) é o guard.
* **Resume keyed-by-workdir (issue #445):** Aider não tem session-id de
  servidor — continuidade via ``.aider.chat.history.md`` no clone reusado;
  ``--restore-chat-history`` recarrega o histórico.
* **list_models dinâmico:** ``aider --list-models ""`` (litellm); fallback para
  catálogo curado quando falha/sem rede.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from dataclasses import replace
from typing import List, Optional

from ._catalog import (OPENROUTER_CLAUDE_SONNET_4_6,
                       OPENROUTER_DEEPSEEK_V4_FLASH,
                       OPENROUTER_DEEPSEEK_V4_PRO, OPENROUTER_QWEN3_CODER)
from .base import (BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult,
                   classify_provider_cutoff)

logger = logging.getLogger("deile.cli_adapters.aider")

#: Timeout (s) do ``aider --list-models`` (toca litellm).
_MODELS_CMD_TIMEOUT_S = 20

#: Padrões de falha estrutural (heurística conservadora — o gate git/test é a autoridade).
_ERROR_MARKERS = (
    "litellm.exceptions",
    "authenticationerror",
    "ratelimiterror",
    "api key",
    "traceback (most recent call last)",
)

#: Catálogo curado de fallback quando ``aider --list-models`` falha/sem rede.
#: A lista dinâmica prevalece; este catálogo garante picker não-vazio.
_FALLBACK_MODELS: List[ModelInfo] = [
    OPENROUTER_DEEPSEEK_V4_FLASH,
    OPENROUTER_DEEPSEEK_V4_PRO,
    # Aider descreve o claude-sonnet como "tarefas cirúrgicas críticas"
    # porque o aider É a frente cirúrgica (auto-commit, edição fina).
    replace(
        OPENROUTER_CLAUDE_SONNET_4_6,
        notes="premium; tarefas cirúrgicas críticas",
    ),
    OPENROUTER_QWEN3_CODER,
]


class AiderAdapter(BaseCliAdapter):
    """Adapter do Aider CLI (worker Tier 1 — cirúrgico, auto-commit)."""

    def build_argv(
        self,
        *,
        brief_path: str,
        model: Optional[str],
        reasoning: Optional[str],
        workdir: str,
        resume: Optional[ResumeCtx],
        task_id: str = "",
    ) -> List[str]:
        """Monta o argv headless do ``aider``.

        Resume (issue #445): Aider é keyed-by-workdir — quando ``resume`` não é
        ``None`` o servidor reusa o clone e ``--restore-chat-history`` recarrega
        ``.aider.chat.history.md``, retomando sem re-gastar tokens. ``reasoning``
        ignorado (sem suporte).
        """
        argv: List[str] = ["aider"]
        if model:
            argv += ["--model", model]
        if resume is not None:
            argv += ["--restore-chat-history"]
        argv += [
            "--message-file", brief_path,
            "--yes-always",
            "--no-stream",
            "--no-pretty",
            "--analytics-disable",
            "--no-check-update",
            "--no-gitignore",
            "--auto-commits",
            # Regra do projeto: sem marca "aider"/Co-Authored-By nos commits.
            "--no-attribute-author",
            "--no-attribute-committer",
            "--no-attribute-commit-message-author",
        ]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME gravável (litellm cache + history do Aider).

        Auth keys vêm do Secret montado no Deployment, não daqui.
        """
        return {"HOME": home}

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Heurística sobre stdout/stderr (Aider não tem JSON headless confiável).

        Exit-code é informativo apenas; o gate de git/test do server decide.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx)
        ANTES da heurística → ``error_code`` específico para o pipeline retomar.
        """
        if (cut := classify_provider_cutoff(stdout, stderr, "aider")):
            return cut

        combined = f"{stdout}\n{stderr}".lower()
        for marker in _ERROR_MARKERS:
            if marker in combined:
                tail = (stderr or stdout)[-2000:].strip()
                return WorkResult(
                    ok=False,
                    result_text=tail or f"aider reportou erro (rc={rc})",
                    error_code="CLI_REPORTED_ERROR",
                )

        tail = (stdout or stderr).strip()
        if tail:
            return WorkResult(ok=True, result_text=tail[-2000:])
        # Sem saída alguma: não confia no rc, mas precisa de veredito; o gate de
        # commit/push do server confirma se o Aider de fato commitou.
        return WorkResult(
            ok=True,
            result_text="aider concluiu sem saída textual; gate de git confirma o commit",
        )

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Aider é keyed-by-workdir (sem session-id de servidor) → ``task_id``.

        Retornar ``task_id`` (não-vazio) sinaliza ao ``resume-info`` que há sessão
        a retomar, disparando reuso do MESMO workdir no próximo dispatch. Sem isto
        o pipeline usaria workdir novo e o histórico ``.aider.chat.history.md``
        se perderia.
        """
        return task_id

    def list_models(self) -> List[ModelInfo]:
        """Dinâmico via ``aider --list-models``; fallback para curado se falhar."""
        dynamic = self._list_models_dynamic()
        return dynamic if dynamic else list(_FALLBACK_MODELS)

    @staticmethod
    def _list_models_dynamic() -> List[ModelInfo]:
        """Tenta listar via ``aider --list-models``; ``[]`` em qualquer falha."""
        if shutil.which("aider") is None:
            return []
        try:
            proc = subprocess.run(
                ["aider", "--list-models", ""],
                capture_output=True,
                text=True,
                timeout=_MODELS_CMD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("`aider --list-models` falhou: %s", exc)
            return []

        models: List[ModelInfo] = []
        seen = set()
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            # O Aider lista com prefixo "- " e linhas-cabeçalho; descarta ruído.
            if line.startswith("- "):
                line = line[2:].strip()
            if not line or "/" not in line or line in seen:
                continue
            if any(ch.isspace() for ch in line):
                continue
            # Linhas-cabeçalho do Aider terminam com ":" (ex.: "Models which match:")
            if line.endswith(":"):
                continue
            seen.add(line)
            provider = line.split("/", 1)[0]
            models.append(ModelInfo(id=line, provider=provider))
        return models


ADAPTER = AiderAdapter(
    kind="aider",
    default_port=8774,
    auth_mode="env",
    supports_resume=True,
    supports_reasoning=False,
    git_strategy="cli_autocommit",
    auth_env_keys=["OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"],
    egress_hosts=["openrouter.ai", "api.deepseek.com"],
    writable_dirs=["HOME"],
    oauth=None,
)


__all__ = ["AiderAdapter", "ADAPTER"]
