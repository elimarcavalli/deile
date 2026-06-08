#!/usr/bin/env python3
"""cli_adapters.qwen — adapter do Qwen Code CLI (frota multi-worker, Tier 2).

Qwen Code é um fork do Gemini CLI (node) focado nos modelos Qwen-Coder; o melhor
custo-benefício da frota. Headless via ``qwen -p "<prompt>"``. Este adapter pluga
o Qwen na frota pelos cinco pontos do contrato
:class:`~cli_adapters.base.CliAdapter`; toda a maquinaria genérica (lease,
heartbeat, subprocess, gate de git, HTTP) vem do ``cli_worker_server`` +
``_worker_core``.

Decisões deste adapter (alinhadas ao plano §2.3/§1.4/§1.6/§1.11 e validadas
contra a doc oficial do Qwen Code via context7 — ``-p``/``--prompt``,
``--output-format json``, ``--approval-mode yolo``/``--yolo``, a tríade
``OPENAI_MODEL``/``OPENAI_BASE_URL``/``OPENAI_API_KEY``):

* **Multi-provider via base_url (§2.3):** Qwen fala OpenAI-compatible; o provider
  é escolhido pela tríade de env (``OPENAI_BASE_URL`` aponta pro Dashscope OU
  OpenRouter OU outro endpoint compatível). O modelo viaja por ``OPENAI_MODEL``,
  não por flag — por isso :meth:`build_argv` NÃO emite ``-m``; o modelo é injetado
  no env pelo servidor (o adapter expõe ``OPENAI_MODEL`` como a env var de modelo).
* **Autonomia (§1.4):** ``--yolo`` (= ``--approval-mode yolo``) + env
  ``QWEN_CODE_UNATTENDED_RETRY=1``; sem TTY qualquer prompt de aprovação travaria
  o subprocess.
* **Brief (§2.3):** o conteúdo do brief é lido do arquivo e passado via ``-p``
  (Qwen não tem ``--message-file``). O timeout do pod capa runs longos (§ custo).
* **Modelo:** via env ``OPENAI_MODEL`` (ex.: ``qwen3-coder-plus``, ou
  ``qwen/qwen3-coder`` quando ``OPENAI_BASE_URL`` aponta pro OpenRouter). O
  servidor injeta o ``OPENAI_MODEL`` resolvido; o adapter não toca o argv com
  modelo.
* **Saída (§1.6):** ``--output-format json`` → JSON estruturado;
  :meth:`parse_output` lê a resposta/erro. Exit-code grosso → o gate de
  commit/push do server decide o sucesso final.
* **list_models:** Qwen não tem comando de listagem confiável → **catálogo
  estático curado** (modelos Qwen-Coder + o que o base_url expõe). Os IDs nativos
  variam com o ``OPENAI_BASE_URL`` configurado; o catálogo cobre os recorrentes.
* **Resume:** ``supports_resume=True`` (issue #445 — anti-sangria de custo).
  Retoma via ``qwen --resume <session_id>`` (session_id capturado dos eventos
  JSON) restaurando history + tool state + compression checkpoints, em vez de
  re-gastar tokens do zero. O brief continua lendo ``.deile-progress.md`` como
  contexto natural complementar.
* **Auth (§1.11/§2.3):** ``env`` — ``OPENAI_API_KEY`` (a tríade OpenAI-compatible;
  OAuth free-tier do Qwen morreu em 2026-04-15). Sem login, sem refresh.
* **Dirs graváveis (§1.7):** ``HOME`` + ``~/.qwen`` (config/cache do CLI node).
  **node>=22** exige imagem própria (Fase C/D — não é responsabilidade do
  adapter). O workdir do repo é gravável por construção.
* **Egress (§1.13):** ``dashscope.aliyuncs.com`` (Dashscope) + ``openrouter.ai``
  (rota OpenRouter). As forges são adicionadas transversalmente pela geração de
  NetworkPolicy.
* **git (§1.5):** ``brief_driven`` — o brief instrui ``git add/commit/push`` sob
  auto-approve; o server valida commit novo + push no gate pós-run.
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

import _worker_core as _core

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.qwen")

#: Teto do ``result_text`` preservando o FIM (onde o veredito de pr_review/refine
#: conclui). Truncar o início cortaria o veredito; mantém os últimos N chars.
_VERDICT_CAP = 12000

#: Catálogo estático curado (Qwen não tem ``list-models`` confiável — §2.3).
#:
#: Fonte: modelos Qwen-Coder do Dashscope + os equivalentes servidos pelo
#: OpenRouter. Os IDs dependem do ``OPENAI_BASE_URL`` configurado no Deployment;
#: o catálogo cobre as duas rotas recorrentes (Dashscope direto = ``qwen3-*``;
#: OpenRouter = ``qwen/qwen3-coder``). Garante picker não-vazio no painel.
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

        Forma: ``qwen -p "<conteúdo do brief>" --yolo --auth-type openai
        --output-format json``.

        ``--auth-type openai`` é OBRIGATÓRIO em modo não-interativo: o qwen-code
        suporta vários backends de auth (``openai``/``anthropic``/``qwen-oauth``/
        ``gemini``/``vertex-ai``) e SEM o flag aborta com "No auth type is
        selected ... before running in non-interactive mode" (homologação E2E do
        stage pr_review). A frota usa a tríade OpenAI-compatible
        (``OPENAI_BASE_URL``/``OPENAI_API_KEY``/``OPENAI_MODEL``) apontando para
        OpenRouter/Dashscope/OpenAI — todos sob o backend ``openai``.

        O modelo entra via ``-m <model>`` (o qwen-code TEM a flag). A injeção por
        ``OPENAI_MODEL`` no env era a intenção original mas nunca foi wirada pelo
        servidor — sem ``-m`` o qwen caía no default ``qwen3.5-plus``, que o
        OpenRouter rejeita (``400 ... is not a valid model ID``), derrubando a
        homologação E2E do pr_review. ``None`` deixa o qwen usar seu default.
        ``reasoning`` é ignorado (sem suporte). O ``workdir`` é o cwd do
        subprocess (definido pelo core).

        **Resume nativo (issue #445):** quando ``resume`` não é ``None``, passa
        ``--resume <session_id>`` para retomar a conversa (history + tool state +
        compression checkpoints restaurados) em vez de re-gastar tokens do zero.
        Forma confirmada na doc oficial do qwen-code (``qwen --resume <id> -p``).
        O id é o ``session_id`` que o qwen emite nos eventos JSON (capturado por
        :meth:`extract_session_id`). Sem resume, roda fresh.
        """
        brief_text = self._read_brief(brief_path)
        argv = ["qwen", "-p", brief_text, "--yolo", "--auth-type", "openai"]
        if resume is not None and resume.session_id:
            argv += ["--resume", resume.session_id]
        if model:
            argv += ["-m", model]
        argv += ["--output-format", "json"]
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
        """Env do subprocess: HOME gravável + retry desatendido + supressão do
        aviso de yolo.

        ``QWEN_CODE_UNATTENDED_RETRY=1`` evita travas em retries que pediriam
        confirmação sem TTY. ``QWEN_CODE_SUPPRESS_YOLO_WARNING=1`` silencia o
        aviso que o qwen-code imprime em modo headless/yolo ("running headless
        with --yolo ... no sandbox") — sem isso o aviso polui o stdout/stderr e o
        parser o lê como veredito, derrubando o dispatch com ``NO_OUTPUT``
        (homologação E2E do stage pr_review). O ``~/.qwen`` (config/cache) fica
        abaixo de ``home``. NÃO inclui ``OPENAI_API_KEY``/``OPENAI_BASE_URL``/
        ``OPENAI_MODEL`` — essas vêm do Secret/ConfigMap montados no Deployment
        (a tríade é configuração de provider, não overlay do adapter).
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

        Qwen emite um objeto JSON (ou JSONL de eventos, conforme versão) ao
        final. :meth:`parse_output` tenta primeiro parsear o stdout inteiro como
        um único objeto JSON; se falhar, varre linha-a-linha (JSONL). Lê o campo
        textual de resposta como veredito; campo/tipo de erro → ``ok=False``.
        Exit-code é informativo apenas (§1.6) — o gate de commit/push do server
        decide o sucesso final. Tolerante a saída malformada.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx/
        conexão) ANTES de qualquer parse — retorna ``error_code`` específico em
        vez de "conclusão limpa" para o pipeline retomar o trabalho parcial.
        """
        provider_err = _core.classify_provider_error(f"{stdout}\n{stderr}")
        if provider_err:
            tail = (stderr or stdout)[-2000:].strip()
            return WorkResult(
                ok=False,
                result_text=tail or f"qwen cortado por provider ({provider_err})",
                error_code=provider_err,
            )

        whole = stdout.strip()
        if whole.startswith("{"):
            try:
                obj = json.loads(whole)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                return self._from_obj(obj, stdout, stderr, rc)

        # Shape atual de ``qwen -p --output-format json``: uma LISTA de eventos
        # ``[{type:system}, {type:assistant}, ..., {type:result}]``. O veredito
        # final está no event ``type=result`` campo ``result``; ``is_error`` (ou
        # um texto "API Error ...") sinaliza falha. Sem isto o parser caía no
        # NO_OUTPUT (o stdout começa com ``[``, não casava nem o dict nem o JSONL)
        # — homologação E2E do pr_review.
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
            return WorkResult(ok=True, result_text=last_text[:2000])
        if saw_event:
            return WorkResult(
                ok=True,
                result_text="qwen concluiu sem veredito textual explícito",
            )
        tail = (stderr or stdout)[-2000:].strip()
        return WorkResult(
            ok=False,
            result_text=tail or f"qwen sem saída parseável (rc={rc})",
            error_code="NO_OUTPUT",
        )

    def _from_events(self, events: list) -> WorkResult:
        """Deriva o :class:`WorkResult` da LISTA de eventos do ``qwen -p``.

        O event ``type=result`` carrega o veredito final em ``result`` e a flag
        ``is_error``. Um ``is_error=True`` OU um texto começando com ``[API
        Error`` / ``API Error`` reprova (ex.: model-id inválido). Sem o event
        ``result`` cai no último texto de ``assistant`` (content[].text).
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
        """Deriva o :class:`WorkResult` de um único objeto JSON do ``qwen``."""
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
        """Extrai o texto de resposta, tolerante ao shape por versão do Qwen."""
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
        """Extrai o ``session_id`` que o qwen emite em TODO evento JSON.

        Com ``--output-format json``, a saída é um array (ou JSONL) de eventos e
        cada um carrega ``session_id`` (o ``system``/``session_start`` o emite
        primeiro), confirmado na doc oficial do qwen-code (dual-output schema).
        Pega o primeiro id não-vazio. Tolera tanto o array único quanto JSONL
        linha-a-linha. Vazio se a saída não trouxe id.
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
        for line in stdout.splitlines():
            line = line.strip()
            if not line or not line.startswith("{"):
                continue
            try:
                ev = json.loads(line)
            except (ValueError, TypeError):
                continue
            sid = self._event_session_id(ev)
            if sid:
                return sid
        return ""

    @staticmethod
    def _event_session_id(event) -> str:
        """``session_id`` de um evento qwen (top-level ou aninhado em ``data``)."""
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
        """Catálogo estático (Qwen não tem ``list-models`` confiável — §2.3)."""
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
