#!/usr/bin/env python3
"""cli_adapters.qwen — adapter do Qwen Code CLI (frota multi-worker, Tier 2).

Qwen Code (fork do Gemini CLI, node) headless via ``qwen -p "<prompt>"``.
Toda maquinaria genérica (lease, heartbeat, subprocess, git gate, HTTP) vem do
``cli_worker_server`` + ``_worker_core``.

Decisões-chave (§2.3/§1.4/§1.6/§1.11, validadas contra doc oficial via context7):

* **Multi-provider:** OpenAI-compatible; tríade ``OPENAI_BASE_URL`` /
  ``OPENAI_API_KEY`` / ``OPENAI_MODEL`` aponta Dashscope, OpenRouter ou outro
  endpoint. O modelo viaja por ``-m <model>`` no argv (ver :meth:`build_argv`).
* **Autonomia:** ``--yolo`` + ``QWEN_CODE_UNATTENDED_RETRY=1`` — sem TTY,
  qualquer prompt de aprovação travaria o subprocess.
* **Auth:** ``OPENAI_API_KEY`` (OAuth free-tier do Qwen morreu em 2026-04-15).
* **Resume (issue #445):** ``qwen --resume <session_id>`` restaura history +
  tool state em vez de re-gastar tokens do zero.
* **Saída:** ``--output-format json``; exit-code é informativo — o gate de
  commit/push do server decide o sucesso final.
* **list_models:** catálogo estático (Qwen não tem ``list-models`` confiável).
* **git:** ``brief_driven`` — o brief instrui ``git add/commit/push``.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from .base import (BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult,
                   classify_provider_cutoff, iter_jsonl_events,
                   no_output_result, read_brief_or_fallback)

logger = logging.getLogger("deile.cli_adapters.qwen")

#: Teto do ``result_text`` preservando o FIM (onde o veredito de pr_review/refine
#: conclui). Truncar o início cortaria o veredito; mantém os últimos N chars.
_VERDICT_CAP = 12000

#: Catálogo estático (Qwen não tem ``list-models`` confiável — §2.3).
#: Cobre Dashscope (``qwen3-*``) + OpenRouter (``qwen/qwen3-coder``).
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3-coder-next",
        label="Qwen3 Coder Next (Dashscope)",
        provider="dashscope",
        price_in=0.11, price_out=0.80,
        notes="MAIS BARATO; MoE esparso 80B/3B-ativos",
    ),
    ModelInfo(
        id="qwen3-coder-plus",
        label="Qwen3 Coder Plus (Dashscope)",
        provider="dashscope",
        price_in=1.00, price_out=5.00,
        notes="melhor de coding (rota Dashscope direta)",
    ),
    ModelInfo(
        id="qwen3-coder-480b-a35b-instruct",
        label="Qwen3 Coder 480B (Dashscope)",
        provider="dashscope",
        price_in=0.22, price_out=1.80, context=1_000_000,
        notes="modelo grande 480B/35B-ativos",
    ),
    ModelInfo(
        id="qwen/qwen3-coder",
        label="Qwen3 Coder 480B (OpenRouter)",
        provider="openrouter",
        price_in=0.22, price_out=1.80, context=1_000_000,
        notes="mesma família via OpenRouter (OPENAI_BASE_URL=openrouter.ai)",
    ),
]


class QwenAdapter(BaseCliAdapter):
    """Adapter do Qwen Code CLI (worker Tier 2 — melhor custo)."""

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
        """Monta o argv headless do ``qwen``.

        Forma: ``qwen -p "<brief>" --yolo --auth-type openai --output-format json``.

        ``--auth-type openai`` é OBRIGATÓRIO em modo não-interativo: sem o flag o
        qwen aborta com "No auth type is selected ... before running in
        non-interactive mode" (homologado no stage pr_review). Todos os backends da
        frota (OpenRouter/Dashscope/OpenAI) usam a tríade OpenAI-compatible.

        ``-m <model>`` é necessário: injetar apenas via ``OPENAI_MODEL`` no env não
        funciona — sem ``-m`` o qwen cai no default ``qwen3.5-plus``, que o
        OpenRouter rejeita com 400 (homologação E2E do pr_review). ``None`` deixa o
        qwen usar seu default. ``reasoning`` é ignorado (sem suporte).

        Resume (issue #445): ``--resume <session_id>`` restaura history + tool
        state em vez de re-gastar tokens do zero.
        """
        brief_text = read_brief_or_fallback(brief_path)
        argv = ["qwen", "-p", brief_text, "--yolo", "--auth-type", "openai"]
        if resume is not None and resume.session_id:
            argv += ["--resume", resume.session_id]
        if model:
            argv += ["-m", model]
        argv += ["--output-format", "json"]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME gravável + retry desatendido + supressão do aviso yolo.

        ``QWEN_CODE_SUPPRESS_YOLO_WARNING=1`` é necessário: sem ele o qwen imprime
        um aviso headless/yolo que polui o stdout e faz o parser retornar
        ``NO_OUTPUT`` (homologado no stage pr_review).
        ``QWEN_CODE_UNATTENDED_RETRY=1`` evita travas em retries sem TTY.
        A tríade ``OPENAI_*`` NÃO entra aqui — vem do Secret/ConfigMap do Deployment.
        """
        return {
            "HOME": home,
            "QWEN_CODE_UNATTENDED_RETRY": "1",
            "QWEN_CODE_SUPPRESS_YOLO_WARNING": "1",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o ``--output-format json`` num :class:`WorkResult`.

        Tenta: objeto JSON único → array de eventos → JSONL linha-a-linha.
        Exit-code é informativo (§1.6); o gate do server decide o sucesso.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx)
        ANTES de qualquer parse para que o pipeline retome em vez de re-gastar.
        """
        if (cut := classify_provider_cutoff(stdout, stderr, "qwen")):
            return cut

        whole = stdout.strip()
        if whole.startswith("{"):
            try:
                obj = json.loads(whole)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                return self._from_obj(obj, stdout, stderr, rc)

        # Shape atual: lista ``[{type:system}, ..., {type:result}]``. O veredito
        # está em ``type=result`` → ``result``/``is_error``. Sem este branch o
        # stdout começando com ``[`` caia em NO_OUTPUT (homologação pr_review).
        if whole.startswith("["):
            try:
                events = json.loads(whole)
            except (ValueError, TypeError):
                events = None
            if isinstance(events, list):
                return self._from_events(events)

        # Fallback: JSONL linha-a-linha.
        last_text = ""
        error_text = ""
        saw_event = False
        for event in iter_jsonl_events(stdout):
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
            return WorkResult(ok=True, result_text=last_text[:2000])
        if saw_event:
            return WorkResult(
                ok=True,
                result_text="qwen concluiu sem veredito textual explícito",
            )
        return no_output_result(stdout, stderr, rc, "qwen")

    def _from_events(self, events: list) -> WorkResult:
        """WorkResult da lista de eventos do ``qwen -p``.

        O event ``type=result`` tem ``result`` (veredito) e ``is_error``.
        ``is_error=True`` ou texto começando com ``[API Error`` reprova
        (ex.: model-id inválido). Sem esse event, usa o último ``assistant``.
        """
        result_ev = next(
            (e for e in reversed(events)
             if isinstance(e, dict) and e.get("type") == "result"),
            None,
        )
        if isinstance(result_ev, dict):
            text = str(result_ev.get("result") or "").strip()
            is_err = bool(result_ev.get("is_error")) or text[:20].lower().lstrip(
                "[ "
            ).startswith("api error")
            if text:
                return WorkResult(
                    ok=not is_err,
                    result_text=text[-_VERDICT_CAP:],
                    error_code="CLI_REPORTED_ERROR" if is_err else None,
                )
        # Sem result event útil → último texto de assistant.
        for ev in reversed(events):
            if isinstance(ev, dict) and ev.get("type") in ("assistant", "message"):
                txt = self._extract_text(ev)
                if txt:
                    return WorkResult(ok=True, result_text=txt[-_VERDICT_CAP:])
        return WorkResult(
            ok=True, result_text="qwen concluiu sem veredito textual explícito",
        )

    def _from_obj(
        self, obj: dict, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """WorkResult de um único objeto JSON do ``qwen``."""
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
            return WorkResult(ok=True, result_text=txt[:2000])
        return WorkResult(
            ok=True, result_text="qwen concluiu sem veredito textual explícito",
        )

    @staticmethod
    def _extract_text(obj: dict) -> str:
        """Texto de resposta, tolerante ao shape por versão do Qwen."""
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
        """Extrai o ``session_id`` dos eventos JSON (dual-output schema, doc oficial).

        Todo evento carrega ``session_id``; o ``system``/``session_start``
        o emite primeiro. Tolera array único, objeto único e JSONL.
        """
        whole = stdout.strip()
        # Array único de eventos.
        if whole.startswith("["):
            try:
                events = json.loads(whole)
            except (ValueError, TypeError):
                events = None
            if isinstance(events, list):
                for ev in events:
                    sid = self._event_session_id(ev)
                    if sid:
                        return sid
        # Objeto único.
        if whole.startswith("{"):
            try:
                obj = json.loads(whole)
            except (ValueError, TypeError):
                obj = None
            sid = self._event_session_id(obj)
            if sid:
                return sid
        # JSONL linha-a-linha.
        for ev in iter_jsonl_events(stdout):
            sid = self._event_session_id(ev)
            if sid:
                return sid
        return ""

    @staticmethod
    def _event_session_id(event) -> str:
        """``session_id`` top-level ou aninhado em ``data``."""
        if not isinstance(event, dict):
            return ""
        sid = event.get("session_id") or event.get("sessionId")
        if isinstance(sid, str) and sid.strip():
            return sid.strip()
        data = event.get("data")
        if isinstance(data, dict):
            nsid = data.get("session_id") or data.get("sessionId")
            if isinstance(nsid, str) and nsid.strip():
                return nsid.strip()
        return ""

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático — Qwen não tem ``list-models`` confiável (§2.3)."""
        return list(_MODELS)


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
ADAPTER = QwenAdapter(
    kind="qwen",
    default_port=8773,
    auth_mode="env",
    supports_resume=True,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENAI_API_KEY"],
    egress_hosts=["dashscope.aliyuncs.com", "openrouter.ai"],
    writable_dirs=["HOME"],
    oauth=None,
)


__all__ = ["QwenAdapter", "ADAPTER"]
