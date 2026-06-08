#!/usr/bin/env python3
"""cli_adapters.goose — adapter do Goose CLI (frota multi-worker, Tier 1).

Goose headless via ``goose run --name <task_id>`` (sessão nomeada para
suportar resume; issue #445). Toda a maquinaria genérica (lease, heartbeat,
subprocess, gate de git, HTTP) vem do ``cli_worker_server`` + ``_worker_core``.

Decisões não-óbvias:

* **Keyring (gotcha):** ``GOOSE_DISABLE_KEYRING=1`` é OBRIGATÓRIO — o Goose
  tenta DBus por padrão, que não existe no pod e quebra o startup.
* **Modelo provider-prefixado:** ``id`` no formato ``<provider>/<modelo>``
  (ex.: ``openrouter/deepseek/deepseek-v4-flash``). ``build_argv`` faz o split
  no PRIMEIRO ``/`` → ``--provider``/``--model``. Sem prefixo o Goose lê o 1º
  segmento como provider e falha "Unknown provider".
* **Teto de turns:** ``--max-turns`` capa custo (não há cap em USD). Default
  :data:`_DEFAULT_MAX_TURNS`, sobreposto por ``DEILE_GOOSE_MAX_TURNS``.
* **Exit-code não-confiável:** o gate de commit/push do server decide o sucesso.
* **GOOSE_MODE=auto falho com ``claude-code``** (issue #3386) — usar com
  OpenRouter/OpenAI (o que o catálogo e o egress assumem).
"""

from __future__ import annotations

import json
import logging
import os
from typing import List, Optional

import _worker_core as _core

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.goose")

#: Teto default de turns (capa custo). Sobreposto por ``DEILE_GOOSE_MAX_TURNS``.
_DEFAULT_MAX_TURNS = 40


def _max_turns() -> int:
    """Teto de turns; valor inválido cai no default :data:`_DEFAULT_MAX_TURNS`."""
    try:
        return int(os.environ.get("DEILE_GOOSE_MAX_TURNS", str(_DEFAULT_MAX_TURNS)))
    except ValueError:
        return _DEFAULT_MAX_TURNS

#: Teto de ``result_text`` preservando o FIM. Veredictos (CLARO/VAGO,
#: REFINO:OK, APPROVE) vivem no FIM — truncar do início cortava o marcador
#: e ``parse_critique_verdict`` caía em "veredito ausente".
_VERDICT_CAP = 12000


def _cap_verdict(text: str) -> str:
    """Trunca ``text`` preservando o FIM (onde o veredito conclui)."""
    t = (text or "").strip()
    return t[-_VERDICT_CAP:]

#: Catálogo estático curado (Goose não tem ``list-models`` confiável).
#: ``id`` é **provider-prefixado** (``<goose-provider>/<modelo>``); ``build_argv``
#: faz o split no PRIMEIRO ``/`` → ``--provider``/``--model``. Sem prefixo o
#: Goose falha "Unknown provider" (não há ``GOOSE_PROVIDER`` no Deployment).
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="openrouter/deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash (OpenRouter)",
        provider="openrouter",
        price_in=0.0983, price_out=0.1966, context=1_048_576,
        notes="MAIS BARATO de coding; default recomendado",
    ),
    ModelInfo(
        id="openrouter/deepseek/deepseek-v4-pro",
        label="DeepSeek V4 Pro (OpenRouter)",
        provider="openrouter",
        price_in=0.435, price_out=0.87, context=1_048_576,
        notes="MELHOR custo-benefício de coding (promo)",
    ),
    ModelInfo(
        id="openrouter/anthropic/claude-sonnet-4.6",
        label="Claude Sonnet 4.6 (OpenRouter)",
        provider="openrouter",
        price_in=3.00, price_out=15.00, context=1_000_000,
        notes="premium; review crítico / arquitetura",
    ),
    ModelInfo(
        id="openrouter/qwen/qwen3-coder",
        label="Qwen3 Coder 480B (OpenRouter)",
        provider="openrouter",
        price_in=0.22, price_out=1.80, context=1_000_000,
        notes="bom custo-benefício p/ implementação",
    ),
    ModelInfo(
        id="openai/gpt-5.4",
        label="GPT-5.4 (OpenAI)",
        provider="openai",
        price_in=2.50, price_out=15.00,
        notes="rota provider=openai; gpt-4o é geração anterior",
    ),
]


class GooseAdapter(BaseCliAdapter):
    """Adapter do Goose CLI (worker Tier 1)."""

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
        """Monta o argv headless do ``goose run``.

        **Sessão nomeada (issue #445):** ``task_id`` É o nome da sessão Goose —
        fresh cria ``--name <task_id>``; resume reabre o MESMO nome com
        ``--resume`` (SQLite persistida). Sem ``task_id`` nem ``session_id``
        cai em ``--no-session`` (efêmero, sem resume — degradação graciosa).

        ``model`` em ``provider/model`` → ``--provider``/``--model`` (split no
        PRIMEIRO ``/``). Sem ``/`` → só ``--model``. ``None`` → env decide.
        ``reasoning`` ignorado (sem suporte).
        """
        brief_text = self._read_brief(brief_path)
        session_name = (resume.session_id if resume is not None else "") or task_id
        argv: List[str] = ["goose", "run"]
        if session_name:
            argv += ["--name", session_name]
            if resume is not None:
                argv += ["--resume"]
        else:
            # Sem identidade de sessão → efêmero (não persiste, sem resume).
            argv += ["--no-session"]
        argv += [
            "--quiet",
            "--output-format", "json",
            "--max-turns", str(_max_turns()),
        ]
        if model:
            if "/" in model:
                provider, model_name = model.split("/", 1)
                argv += ["--provider", provider, "--model", model_name]
            else:
                argv += ["--model", model]
        argv += ["-t", brief_text]
        return argv

    @staticmethod
    def _read_brief(brief_path: str) -> str:
        """Lê o conteúdo do brief; em falha de I/O cai num prompt mínimo."""
        try:
            with open(brief_path, "r", encoding="utf-8") as fh:
                return fh.read()
        except OSError as exc:
            logger.warning("não consegui ler o brief %r: %s", brief_path, exc)
            return (
                f"Leia o brief em {brief_path} e implemente exatamente o que ele "
                "descreve. Faça git add/commit/push das mudanças ao terminar."
            )

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME/XDG graváveis + auto-mode + keyring desligado.

        ``GOOSE_DISABLE_KEYRING=1`` é OBRIGATÓRIO — DBus não existe no pod e
        quebra o startup. Auth keys e ``GOOSE_PROVIDER``/``GOOSE_MODEL`` vêm do
        Secret/Deployment, não daqui.
        """
        return {
            "HOME": home,
            "XDG_CONFIG_HOME": f"{home}/.config",
            "GOOSE_MODE": "auto",
            "GOOSE_DISABLE_KEYRING": "1",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o ``--output-format json`` num :class:`WorkResult`.

        Tenta parsear stdout inteiro como objeto; se falhar, varre JSONL linha
        a linha. Exit-code não-confiável — o gate de git/push do server decide.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx)
        ANTES do parse → ``error_code`` específico para o pipeline retomar.
        """
        provider_err = _core.classify_provider_error(f"{stdout}\n{stderr}")
        if provider_err:
            tail = (stderr or stdout)[-2000:].strip()
            return WorkResult(
                ok=False,
                result_text=tail or f"goose cortado por provider ({provider_err})",
                error_code=provider_err,
            )

        whole = stdout.strip()
        if whole.startswith("{"):
            try:
                obj = json.loads(whole)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                return self._from_obj(obj)

        last_text = ""
        error_text = ""
        saw_event = False
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                event = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(event, dict):
                continue
            saw_event = True
            etype = str(event.get("type", ""))
            if "error" in etype.lower() or event.get("error"):
                error_text = self._extract_text(event) or error_text
                continue
            txt = self._extract_text(event)
            if txt:
                last_text = txt

        if error_text:
            return WorkResult(
                ok=False, result_text=error_text[:2000],
                error_code="CLI_REPORTED_ERROR",
            )
        if last_text:
            return WorkResult(ok=True, result_text=_cap_verdict(last_text))
        if saw_event:
            return WorkResult(
                ok=True,
                result_text="goose concluiu sem veredito textual explícito",
            )
        tail = (stderr or stdout)[-2000:].strip()
        return WorkResult(
            ok=False,
            result_text=tail or f"goose sem saída parseável (rc={rc})",
            error_code="NO_OUTPUT",
        )

    def _from_obj(self, obj: dict) -> WorkResult:
        """Deriva o :class:`WorkResult` de um único objeto JSON do ``goose``."""
        err = obj.get("error")
        if err:
            txt = self._extract_text(obj) or (
                err if isinstance(err, str) else json.dumps(err)
            )
            return WorkResult(
                ok=False, result_text=str(txt)[:2000],
                error_code="CLI_REPORTED_ERROR",
            )
        txt = self._extract_text(obj)
        if txt:
            return WorkResult(ok=True, result_text=_cap_verdict(txt))
        return WorkResult(
            ok=True, result_text="goose concluiu sem veredito textual explícito",
        )

    @staticmethod
    def _extract_text(obj: dict) -> str:
        """Extrai o texto de resposta, tolerante ao shape por versão do Goose.

        Shape >=1.3x: ``{"messages": [...]}`` — veredito na ÚLTIMA mensagem
        ``role=assistant``, em ``content[].text`` de blocos ``type=="text"``
        (blocos ``thinking`` são ignorados). Sem isto o parser cai no fallback
        "sem veredito textual" e ``parse_critique_verdict`` nunca vê CLARO/VAGO.
        Mantém fallback para chaves top-level de versões antigas.
        """
        msgs = obj.get("messages")
        if isinstance(msgs, list) and msgs:
            for msg in reversed(msgs):
                if not isinstance(msg, dict):
                    continue
                content = msg.get("content")
                if isinstance(content, list):
                    parts = [
                        c.get("text", "")
                        for c in content
                        if isinstance(c, dict)
                        and c.get("type") == "text"
                        and isinstance(c.get("text"), str)
                        and c.get("text").strip()
                    ]
                    if parts:
                        return "\n".join(parts).strip()
                elif isinstance(content, str) and content.strip():
                    return content.strip()
        # Fallback — shapes antigos com o texto numa chave top-level.
        for key in ("response", "result", "text", "message", "content", "output"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                nested = val.get("text") or val.get("content") or val.get("message")
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Sessão nomeada determinística → session-id É o ``task_id``.

        Retornar ``task_id`` (não-vazio) sinaliza ao ``resume-info`` que há sessão
        a retomar, disparando reuso do workdir + ``--resume`` no próximo dispatch.
        """
        return task_id

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático (Goose não tem ``list-models`` confiável)."""
        return list(_MODELS)


ADAPTER = GooseAdapter(
    kind="goose",
    default_port=8775,
    auth_mode="env",
    supports_resume=True,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
    egress_hosts=["openrouter.ai", "api.openai.com"],
    writable_dirs=["HOME", "XDG_CONFIG_HOME"],
    oauth=None,
)


__all__ = ["GooseAdapter", "ADAPTER"]
