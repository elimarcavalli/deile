#!/usr/bin/env python3
"""cli_adapters.goose — adapter do Goose CLI (frota multi-worker, Tier 1).

Goose é o agente de coding open-source da Block (binário rust). Headless via
``goose run --name <task_id>`` (sessão nomeada determinística para suportar
resume; issue #445). Este adapter pluga o Goose na frota pelos cinco
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
* **Resume:** ``supports_resume=True`` (issue #445 — anti-sangria de custo).
  Substitui o ``--no-session`` por sessão NOMEADA determinística
  (``--name <task_id>``); o resume reabre o mesmo nome com ``--resume``,
  retomando a sessão SQLite persistida em vez de re-gastar do zero. O brief
  continua lendo ``.deile-progress.md`` como contexto natural complementar.
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

import _worker_core as _core

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.goose")

#: Teto default de turns headless (capa custo; o server pode sobrepor por env).
_DEFAULT_MAX_TURNS = 40

#: Teto do ``result_text`` preservando o FIM do texto. Os veredictos das fases
#: de julgamento (crítica CLARO/VAGO, refine REFINO:OK, pr_review APPROVE) são
#: conclusivos — vivem no FIM de uma análise potencialmente longa. Truncar o
#: início (``[:N]``) cortava o veredito e ``parse_critique_verdict`` caía em
#: "veredito ausente" (homologação E2E do stage refine). Mantém os últimos N
#: chars; folga p/ uma crítica longa + o marcador final.
_VERDICT_CAP = 12000


def _cap_verdict(text: str) -> str:
    """Trunca preservando o FIM (onde o veredito conclui), não o início."""
    t = (text or "").strip()
    return t[-_VERDICT_CAP:]

#: Catálogo estático curado (Goose não tem ``list-models`` confiável — §2.5).
#:
#: Fonte: modelos OpenRouter/OpenAI de uso recorrente. Os IDs nativos dependem do
#: ``GOOSE_PROVIDER`` configurado no Deployment; o catálogo cobre a rota
#: recomendada (OpenRouter). Garante picker não-vazio no painel.
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="deepseek/deepseek-v4-flash",
        label="DeepSeek V4 Flash (OpenRouter)",
        provider="openrouter",
        price_in=0.0983, price_out=0.1966, context=1_048_576,
        notes="MAIS BARATO de coding; default recomendado (GOOSE_PROVIDER=openrouter)",
    ),
    ModelInfo(
        id="deepseek/deepseek-v4-pro",
        label="DeepSeek V4 Pro (OpenRouter)",
        provider="openrouter",
        price_in=0.435, price_out=0.87, context=1_048_576,
        notes="MELHOR custo-benefício de coding (promo)",
    ),
    ModelInfo(
        id="anthropic/claude-sonnet-4.6",
        label="Claude Sonnet 4.6 (OpenRouter)",
        provider="openrouter",
        price_in=3.00, price_out=15.00, context=1_000_000,
        notes="premium; review crítico / arquitetura",
    ),
    ModelInfo(
        id="qwen/qwen3-coder",
        label="Qwen3 Coder 480B (OpenRouter)",
        provider="openrouter",
        price_in=0.22, price_out=1.80, context=1_000_000,
        notes="bom custo-benefício p/ implementação",
    ),
    ModelInfo(
        id="gpt-5.4",
        label="GPT-5.4 (OpenAI)",
        provider="openai",
        price_in=2.50, price_out=15.00,
        notes="rota GOOSE_PROVIDER=openai; gpt-4o é geração anterior",
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

        Forma fresh: ``goose run --name <task_id> --quiet --output-format json
        --max-turns <teto> [--provider <p> --model <m>] -t "<conteúdo do brief>"``.

        Forma resume (issue #445): ``goose run --name <session_id> --resume ...``
        — retoma a sessão NOMEADA persistida (SQLite) em vez de re-gastar tokens
        do zero. Formas confirmadas na doc oficial do Goose (``goose run -n <name>``
        / ``goose run -n <name> --resume``).

        **Sessão nomeada determinística:** o ``task_id`` (que nós controlamos) É o
        nome/session-id da sessão Goose — fresh cria ``--name <task_id>``, resume
        reabre o MESMO nome com ``--resume``. Isto substitui o ``--no-session``
        antigo (que não persistia nada e impossibilitava resume). Quando o
        ``task_id`` não é fornecido (dublês antigos) ou no resume usamos o
        ``resume.session_id`` (== prev task_id); sem nenhum dos dois, cai em
        ``--no-session`` (fresh efêmero, sem resume — degradação graciosa).

        Quando ``model`` vem no formato ``provider/model``, mapeia os dois lados
        para ``--provider``/``--model``; um ``model`` sem ``/`` vira só ``--model``;
        ``None`` deixa ``GOOSE_PROVIDER``/``GOOSE_MODEL`` do env decidirem.
        ``reasoning`` é ignorado (sem suporte). O ``workdir`` é o cwd do
        subprocess (definido pelo core), não uma flag.
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

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx/
        conexão) ANTES do parse — retorna ``error_code`` específico em vez de
        "conclusão limpa" para o pipeline retomar o trabalho parcial.
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

        Shape atual de ``goose run --output-format json`` (>=1.3x):
        ``{"messages": [...], "metadata": {...}}`` — o veredito está na ÚLTIMA
        mensagem ``role=assistant``, em ``content[].text`` dos blocos
        ``type=="text"`` (há também blocos ``type=="thinking"`` que ignoramos).
        Sem isto o parser caía no fallback "sem veredito textual" e o
        ``parse_critique_verdict`` do pipeline nunca via CLARO/VAGO (homologação
        E2E do stage refine). Mantém o fallback para chaves top-level de versões
        antigas.
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
        """Goose usa sessão NOMEADA determinística → o session-id É o ``task_id``.

        Como :meth:`build_argv` cria a sessão com ``--name <task_id>``, o nome (=
        session-id que ``--resume`` reabre) é o próprio ``task_id`` — não há que
        parsear da saída. Retornar o ``task_id`` faz o ``resume-info`` sinalizar
        "há sessão a retomar", disparando o reuso do MESMO workdir + ``--resume``
        no próximo dispatch.
        """
        return task_id

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático (Goose não tem ``list-models`` confiável — §2.5)."""
        return list(_MODELS)


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
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
