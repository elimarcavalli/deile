#!/usr/bin/env python3
"""cli_adapters.opencode — adapter do OpenCode (worker piloto da frota — Tier 1).

OpenCode é um agente de coding agnóstico de provider, distribuído como binário
standalone. Headless via ``opencode run`` (sem TUI). Pluga o OpenCode na frota
multi-worker pelos cinco pontos do contrato :class:`~cli_adapters.base.CliAdapter`;
a maquinaria genérica (lease, heartbeat, subprocess, gate de git, HTTP) vem do
``cli_worker_server`` + ``_worker_core``.

Decisões (§1.4/§1.7/§2.1 — validadas contra doc oficial via context7):

* **Autonomia (§1.4):** ``--dangerously-skip-permissions`` **+** config inline
  ``{"permission":{"*":"allow"}}`` via ``OPENCODE_CONFIG_CONTENT``. Belt-and-
  suspenders: se a flag não existir na versão pinada, a config sozinha libera.
* **Brief (§2.1):** ``-f <brief_path>`` (``--file``) + instrução posicional.
  ``stdin`` não é suportado no OpenCode; ``-f`` é o caminho oficial.
* **Modelo:** ``-m provider/model`` (ex. ``openrouter/anthropic/claude-sonnet-4.6``);
  ``None`` → default da config/conta.
* **Saída (§1.6):** ``--format json`` → NDJSON (``step_start``/``step_finish``/
  ``tool_use``/``text``). Exit-code não é confiável; gate de commit/push decide.
* **list_models:** dinâmico via ``opencode models``, com fallback curado quando
  o comando falha ou está sem rede (server cacheia com TTL).
* **Resume (issue #445):** ``--session <id>`` retoma sessão anterior sem re-gastar
  tokens. ``sessionID`` capturado do NDJSON (ver :meth:`extract_session_id`).
* **Auth (§1.11/§2.1):** ``env`` — ``OPENROUTER_API_KEY`` (sem login/refresh).
* **Dirs graváveis (§1.7):** ``HOME`` + XDG dirs; config inline dispensa arquivo.
* **Egress (§1.13):** ``openrouter.ai`` (LLM) + ``models.dev`` (catálogo). Forges
  adicionadas transversalmente pela NetworkPolicy.
* **git (§1.5):** ``brief_driven`` — brief instrui git; server valida no gate.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess
from typing import List, Optional

from dataclasses import replace

from ._catalog import (OPENROUTER_CLAUDE_SONNET_4_6,
                       OPENROUTER_DEEPSEEK_V4_FLASH,
                       OPENROUTER_DEEPSEEK_V4_PRO, OPENROUTER_QWEN3_CODER)
from .base import (BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult,
                   classify_provider_cutoff, iter_jsonl_events,
                   no_output_result)

logger = logging.getLogger("deile.cli_adapters.opencode")

#: Config inline via ``OPENCODE_CONFIG_CONTENT``. ``permission: {"*": "allow"}``
#: libera toda tool sem prompt — essencial sem TTY. Schema confirmado na doc oficial.
_AUTONOMY_CONFIG = {
    "$schema": "https://opencode.ai/config.json",
    "permission": {"*": "allow"},
}

#: Timeout (s) do ``opencode models`` em :meth:`list_models` (toca models.dev).
_MODELS_CMD_TIMEOUT_S = 20

#: Catálogo curado de fallback quando ``opencode models`` falha/sem rede. IDs no
#: formato nativo (``provider/model``). A lista dinâmica prevalece; este garante
#: que o picker do painel nunca fica vazio.
_FALLBACK_MODELS: List[ModelInfo] = [
    # Opencode usa uma variante mais específica do default do flash; outros
    # campos vêm do catálogo compartilhado (preserva preço unificado).
    replace(
        OPENROUTER_DEEPSEEK_V4_FLASH,
        notes="MAIS BARATO de coding; default recomendado p/ o grosso",
    ),
    replace(
        OPENROUTER_DEEPSEEK_V4_PRO,
        notes="MELHOR custo-benefício de coding (promo; sobe p/ $1.74/$3.48)",
    ),
    OPENROUTER_CLAUDE_SONNET_4_6,
    OPENROUTER_QWEN3_CODER,
    ModelInfo(
        id="openrouter/qwen/qwen3-coder-next",
        label="Qwen3 Coder Next (OpenRouter)",
        provider="openrouter",
        price_in=0.11, price_out=0.80,
        notes="MoE esparso 80B/3B-ativos; barato p/ coding",
    ),
    ModelInfo(
        # gpt-4.1 saiu das versões datadas do OpenRouter (deprecação OpenAI
        # out/2026); gpt-5.5 é o substituto premium de coding. Fonte:
        # https://openrouter.ai/openai/gpt-5.5 (verif. 2026-06-07)
        id="openrouter/openai/gpt-5.5",
        label="GPT-5.5 (OpenRouter)",
        provider="openrouter",
        notes="premium; tarefas complexas / review crítico",
    ),
]


class OpenCodeAdapter(BaseCliAdapter):
    """Adapter do OpenCode CLI (worker piloto)."""

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
        """Monta o argv headless do ``opencode run``.

        Forma: ``opencode run --dir <workdir> [-m <model>] [--variant <r>]
        [--session <id>] --dangerously-skip-permissions --format json
        "<instrução posicional>" -f <brief_path>``.

        **Resume (issue #445):** ``--session <session_id>`` retoma conversa nativa
        sem re-gastar tokens. Flag confirmada na doc oficial (``run --session``/``-s``).

        ORDEM CRÍTICA (homologação E2E): ``-f``/``--file`` é ``[array]`` no opencode
        (>=1.16); yargs é GULOSO e consome todos os tokens não-flag seguintes. Se
        ``-f`` viesse antes da instrução posicional, o array engoliria a instrução
        como arquivo (``File not found: "Implemente..."``). A mensagem posicional
        vem ANTES e ``-f <brief_path>`` é o ÚLTIMO token.
        """
        argv: List[str] = ["opencode", "run", "--dir", workdir]
        if model:
            argv += ["-m", model]
        if reasoning:  # supports_reasoning=False; guarda defensiva
            argv += ["--variant", reasoning]
        if resume is not None and resume.session_id:
            argv += ["--session", resume.session_id]
        argv += [
            "--dangerously-skip-permissions",
            "--format", "json",
            "Implemente exatamente o que o brief anexado descreve. "
            "Faça git add/commit/push das mudanças ao terminar.",
            "-f", brief_path,
        ]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME/XDG graváveis + config de autonomia inline.

        ``OPENCODE_CONFIG_CONTENT`` injeta a config sem tocar disco (compatível
        com ``readOnlyRootFilesystem``). Não inclui ``auth_env_keys`` — vêm do
        Secret do Deployment.
        """
        return {
            "HOME": home,
            "XDG_DATA_HOME": f"{home}/.local/share",
            "XDG_CONFIG_HOME": f"{home}/.config",
            "XDG_CACHE_HOME": f"{home}/.cache",
            "OPENCODE_CONFIG_CONTENT": json.dumps(
                _AUTONOMY_CONFIG, separators=(",", ":"),
            ),
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o NDJSON de ``--format json`` num :class:`WorkResult`.

        Veredito = último evento ``text``; erro estruturado (``type`` contendo
        ``error``) → ``ok=False``. Exit-code informativo; gate de commit/push do
        server decide o sucesso final.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx/
        conexão) ANTES da heurística — retorna ``error_code`` específico, nunca
        "conclusão limpa" (bug opencode #629: 402 mid-task marcado completo).
        """
        if (cut := classify_provider_cutoff(stdout, stderr, "opencode")):
            return cut

        last_text = ""
        error_text = ""
        saw_event = False

        for event in iter_jsonl_events(stdout):
            saw_event = True
            etype = str(event.get("type", ""))
            if "error" in etype.lower():
                error_text = self._event_text(event) or error_text
            elif etype == "text" or "text" in event:
                txt = self._event_text(event)
                if txt:
                    last_text = txt

        if error_text:
            return WorkResult(
                ok=False,
                result_text=error_text[:2000],
                error_code="CLI_REPORTED_ERROR",
            )

        if last_text:
            return WorkResult(ok=True, result_text=last_text[:2000])

        if saw_event:
            # Houve eventos mas nenhum texto — gate de git do server confirma.
            return WorkResult(
                ok=True,
                result_text="opencode concluiu sem veredito textual explícito",
            )
        return no_output_result(stdout, stderr, rc, "opencode")

    @staticmethod
    def _event_text(event: dict) -> str:
        """Texto de um evento NDJSON — tenta chaves conhecidas, tolerante ao shape por versão."""
        for key in ("text", "message", "content", "data"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                nested = val.get("text") or val.get("content")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Extrai o ``sessionID`` do NDJSON (confirmado em ``run.ts``).

        Todo evento carrega ``sessionID``; pega o primeiro não-vazio (todos
        compartilham a mesma sessão). Vazio se run abortou antes do primeiro
        evento — server não persiste id e próximo dispatch cai em fresh.
        """
        for event in iter_jsonl_events(stdout):
            sid = event.get("sessionID") or event.get("sessionId")
            if isinstance(sid, str) and sid.strip():
                return sid.strip()
            for nest_key in ("data", "session"):  # alguns eventos aninham o id
                nest = event.get(nest_key)
                if isinstance(nest, dict):
                    nsid = nest.get("sessionID") or nest.get("id")
                    if isinstance(nsid, str) and nsid.strip():
                        return nsid.strip()
        return ""

    def list_models(self) -> List[ModelInfo]:
        """Dinâmico via ``opencode models``; cai em :data:`_FALLBACK_MODELS` se falhar.

        Pode tocar a rede (``models.dev``); o ``cli_worker_server`` cacheia o resultado.
        """
        dynamic = self._list_models_dynamic()
        return dynamic if dynamic else list(_FALLBACK_MODELS)

    @staticmethod
    def _list_models_dynamic() -> List[ModelInfo]:
        """``opencode models`` → lista; ``[]`` em qualquer falha."""
        if shutil.which("opencode") is None:
            return []
        try:
            proc = subprocess.run(
                ["opencode", "models"],
                capture_output=True,
                text=True,
                timeout=_MODELS_CMD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("`opencode models` falhou: %s", exc)
            return []

        models: List[ModelInfo] = []
        seen = set()
        for raw in proc.stdout.splitlines():
            mid = raw.strip()
            if not mid or "/" not in mid or mid in seen:
                continue
            if any(ch.isspace() for ch in mid):
                continue
            seen.add(mid)
            provider = mid.split("/", 1)[0]
            models.append(ModelInfo(id=mid, provider=provider))
        return models


ADAPTER = OpenCodeAdapter(
    kind="opencode",
    default_port=8771,
    auth_mode="env",
    supports_resume=True,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENROUTER_API_KEY"],
    egress_hosts=["openrouter.ai", "models.dev"],
    writable_dirs=["HOME", "XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"],
    oauth=None,
)


__all__ = ["OpenCodeAdapter", "ADAPTER"]
