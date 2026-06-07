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
* **Resume:** ``supports_resume=False`` (fresh-only; o brief lê
  ``.deile-progress.md`` para contexto natural).
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

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.qwen")

#: Catálogo estático curado (Qwen não tem ``list-models`` confiável — §2.3).
#:
#: Fonte: modelos Qwen-Coder do Dashscope + os equivalentes servidos pelo
#: OpenRouter. Os IDs dependem do ``OPENAI_BASE_URL`` configurado no Deployment;
#: o catálogo cobre as duas rotas recorrentes (Dashscope direto = ``qwen3-*``;
#: OpenRouter = ``qwen/qwen3-coder``). Garante picker não-vazio no painel.
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="qwen3-coder-plus",
        label="Qwen3 Coder Plus (Dashscope)",
        provider="dashscope",
        notes="melhor custo-benefício p/ implementação (rota Dashscope direta)",
    ),
    ModelInfo(
        id="qwen3-coder-next",
        label="Qwen3 Coder Next (Dashscope)",
        provider="dashscope",
    ),
    ModelInfo(
        id="qwen3-coder-480b-a35b-instruct",
        label="Qwen3 Coder 480B (Dashscope)",
        provider="dashscope",
        notes="modelo grande; mais caro",
    ),
    ModelInfo(
        id="qwen/qwen3-coder",
        label="Qwen3 Coder (OpenRouter)",
        provider="openrouter",
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
    ) -> List[str]:
        """Monta o argv headless do ``qwen``.

        Forma: ``qwen -p "<conteúdo do brief>" --yolo --output-format json``.

        O modelo NÃO entra no argv — viaja por ``OPENAI_MODEL`` no env (a tríade
        OpenAI-compatible do Qwen). ``model`` aqui é aceito por compat de
        assinatura mas não emite flag; o servidor injeta ``OPENAI_MODEL``.
        ``reasoning`` e ``resume`` são ignorados (sem suporte). O ``workdir`` é o
        cwd do subprocess (definido pelo core), não uma flag.
        """
        brief_text = self._read_brief(brief_path)
        return [
            "qwen",
            "-p", brief_text,
            "--yolo",
            "--output-format", "json",
        ]

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
        """
        whole = stdout.strip()
        if whole.startswith("{"):
            try:
                obj = json.loads(whole)
            except (ValueError, TypeError):
                obj = None
            if isinstance(obj, dict):
                return self._from_obj(obj, stdout, stderr, rc)

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

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático (Qwen não tem ``list-models`` confiável — §2.3)."""
        return list(_MODELS)


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
ADAPTER = QwenAdapter(
    kind="qwen",
    default_port=8773,
    auth_mode="env",
    supports_resume=False,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENAI_API_KEY"],
    egress_hosts=["dashscope.aliyuncs.com", "openrouter.ai"],
    writable_dirs=["HOME"],
    oauth=None,
)


__all__ = ["QwenAdapter", "ADAPTER"]
