#!/usr/bin/env python3
"""cli_adapters.goose — adapter do Goose CLI (frota multi-worker, Tier 1).

Goose é o agente de coding open-source da Block (binário rust). Headless via
``goose run --no-session``. Este adapter pluga o Goose na frota pelos cinco
pontos do contrato :class:`~cli_adapters.base.CliAdapter`; toda a maquinaria
genérica (lease, heartbeat, subprocess, gate de git, HTTP) vem do
``cli_worker_server`` + ``_worker_core``.

Decisões deste adapter (alinhadas ao plano §2.5/§1.4/§1.6/§1.11 e validadas
contra a doc oficial do Goose via context7 — ``goose run --no-session -t``,
``--output-format json``, ``--max-turns``, ``--quiet``, e as env vars
``GOOSE_MODE=auto``/``GOOSE_PROVIDER``/``GOOSE_MODEL``/``GOOSE_DISABLE_KEYRING``):

* **Autonomia (§1.4):** env ``GOOSE_MODE=auto`` — aprova operações
  automaticamente; sem TTY qualquer confirmação travaria o subprocess.
* **Keyring (§1.7/§2.5 gotcha):** ``GOOSE_DISABLE_KEYRING=1`` é OBRIGATÓRIO — o
  Goose tenta o keyring do SO (DBus) por padrão, que não existe no pod e quebra
  o startup. Com a env var, ele lê a chave do provider do ambiente.
* **Teto de turns (§ custo):** ``--max-turns <teto>`` baixo capa o custo (não há
  cap em USD nesses CLIs). Default conservador; o server pode sobrepor por env.
* **Brief (§2.5):** o conteúdo do brief é lido do arquivo e passado via ``-t``
  (texto da instrução). Goose também aceita ``--instructions <arquivo>``/``-i -``
  (stdin), mas ``-t`` com o texto inline é o caminho determinístico headless.
* **Modelo (§2.5):** via env ``GOOSE_PROVIDER`` + ``GOOSE_MODEL`` (configuração
  de Deployment) — NÃO entra no argv. ``build_argv`` aceita ``--provider``/
  ``--model`` por invocação quando um ``model`` no formato ``provider/model`` é
  passado, mapeando os dois lados; ``None`` deixa o env decidir.
* **Saída (§1.6):** ``--output-format json`` → JSON/JSONL de eventos;
  :meth:`parse_output` lê a resposta/erro. Exit-code não-confiável (§2.5 gotcha)
  → o gate de commit/push do server decide o sucesso final.
* **list_models:** Goose não tem comando de listagem confiável → **catálogo
  estático curado** (OpenRouter/OpenAI). Os IDs nativos dependem do
  ``GOOSE_PROVIDER`` configurado.
* **Resume:** ``supports_resume=False`` (``--no-session``; o brief lê
  ``.deile-progress.md`` para contexto natural).
* **Auth (§1.11/§2.5):** ``env`` — ``OPENROUTER_API_KEY`` (rota recomendada, uma
  chave → vários providers) ou ``OPENAI_API_KEY``. Sem login, sem refresh.
* **Dirs graváveis (§1.7):** ``HOME`` + ``XDG_CONFIG_HOME`` (``~/.config/goose``).
  Instalar com ``CONFIGURE=false`` (responsabilidade da imagem, Fase C/D). O
  workdir do repo é gravável por construção.
* **Egress (§1.13):** ``openrouter.ai`` + ``api.openai.com``. As forges são
  adicionadas transversalmente pela geração de NetworkPolicy.
* **git (§1.5):** ``brief_driven`` — a Developer extension dá shell + text_editor;
  o brief instrui ``git add/commit/push``; o server valida no gate pós-run.
* **Gotcha (§2.5):** ``GOOSE_MODE=auto`` reportado falho com provider
  ``claude-code`` (issue #3386) — usar com OpenRouter/OpenAI (o que o catálogo
  e o egress assumem).
"""

from __future__ import annotations

import json
import logging
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.goose")

#: Teto default de turns headless (capa custo; o server pode sobrepor por env).
_DEFAULT_MAX_TURNS = 40

#: Catálogo estático curado (Goose não tem ``list-models`` confiável — §2.5).
#:
#: Fonte: modelos OpenRouter/OpenAI de uso recorrente. Os IDs nativos dependem do
#: ``GOOSE_PROVIDER`` configurado no Deployment; o catálogo cobre a rota
#: recomendada (OpenRouter). Garante picker não-vazio no painel.
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="anthropic/claude-sonnet-4",
        label="Claude Sonnet 4 (OpenRouter)",
        provider="openrouter",
        notes="rota recomendada GOOSE_PROVIDER=openrouter",
    ),
    ModelInfo(
        id="deepseek/deepseek-chat",
        label="DeepSeek Chat (OpenRouter)",
        provider="openrouter",
        notes="barato; default recomendado p/ o grosso do trabalho",
    ),
    ModelInfo(
        id="qwen/qwen3-coder",
        label="Qwen3 Coder (OpenRouter)",
        provider="openrouter",
    ),
    ModelInfo(
        id="gpt-4o",
        label="GPT-4o (OpenAI)",
        provider="openai",
        notes="rota GOOSE_PROVIDER=openai",
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
    ) -> List[str]:
        """Monta o argv headless do ``goose run``.

        Forma: ``goose run --no-session --quiet --output-format json
        --max-turns <teto> [--provider <p> --model <m>] -t "<conteúdo do brief>"``.

        Quando ``model`` vem no formato ``provider/model``, mapeia os dois lados
        para ``--provider``/``--model`` (sobrepõe o env por invocação); um
        ``model`` sem ``/`` vira só ``--model`` (provider fica a cargo do env);
        ``None`` deixa ``GOOSE_PROVIDER``/``GOOSE_MODEL`` do env decidirem.
        ``reasoning`` e ``resume`` são ignorados (sem suporte). O ``workdir`` é o
        cwd do subprocess (definido pelo core), não uma flag.
        """
        brief_text = self._read_brief(brief_path)
        argv: List[str] = [
            "goose", "run",
            "--no-session",
            "--quiet",
            "--output-format", "json",
            "--max-turns", str(_DEFAULT_MAX_TURNS),
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

        ``GOOSE_MODE=auto`` (autonomia sem TTY) e ``GOOSE_DISABLE_KEYRING=1``
        (OBRIGATÓRIO — DBus/keyring quebra no pod). ``XDG_CONFIG_HOME`` aponta
        para baixo de ``home`` (``~/.config/goose``). NÃO inclui ``auth_env_keys``
        (``OPENROUTER_API_KEY``/``OPENAI_API_KEY`` vêm do Secret montado no
        Deployment) nem ``GOOSE_PROVIDER``/``GOOSE_MODEL`` (configuração de
        Deployment).
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

        Goose pode emitir um objeto JSON único ou JSONL de eventos conforme a
        versão. Tenta primeiro parsear o stdout inteiro como objeto; se falhar,
        varre linha-a-linha (JSONL), lendo o último campo textual como veredito.
        Tipo/campo de erro → ``ok=False``. Exit-code não-confiável (§2.5) → o gate
        de commit/push do server decide o sucesso final. Tolerante a saída
        malformada.
        """
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
            return WorkResult(ok=True, result_text=last_text[:2000])
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
            return WorkResult(ok=True, result_text=txt[:2000])
        return WorkResult(
            ok=True, result_text="goose concluiu sem veredito textual explícito",
        )

    @staticmethod
    def _extract_text(obj: dict) -> str:
        """Extrai o texto de resposta, tolerante ao shape por versão do Goose."""
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
        """Catálogo estático (Goose não tem ``list-models`` confiável — §2.5)."""
        return list(_MODELS)


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
ADAPTER = GooseAdapter(
    kind="goose",
    default_port=8775,
    auth_mode="env",
    supports_resume=False,
    supports_reasoning=False,
    git_strategy="brief_driven",
    auth_env_keys=["OPENROUTER_API_KEY", "OPENAI_API_KEY"],
    egress_hosts=["openrouter.ai", "api.openai.com"],
    writable_dirs=["HOME", "XDG_CONFIG_HOME"],
    oauth=None,
)


__all__ = ["GooseAdapter", "ADAPTER"]
