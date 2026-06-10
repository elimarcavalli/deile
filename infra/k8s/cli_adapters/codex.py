#!/usr/bin/env python3
"""cli_adapters.codex — adapter do OpenAI Codex CLI (frota multi-worker, Tier 2).

Codex é o agente de coding headless da OpenAI (binário rust). Headless via
``codex exec`` (NUNCA ``codex`` puro — pode panicar sem TTY). Pluga o Codex na
frota pelos cinco pontos do contrato :class:`~cli_adapters.base.CliAdapter`; a
maquinaria genérica (lease, heartbeat, subprocess, gate de git, HTTP) vem do
``cli_worker_server`` + ``_worker_core``.

Decisões (§2.2/§1.4/§1.6/§1.11 — validadas contra doc oficial via context7):

* **OpenAI DIRETO, não OpenRouter (§2.2):** Codex exige ``wire_api="responses"``
  (Responses API); OpenRouter só fala Chat Completions → incompatível.
* **Autonomia (§1.4):** ``--dangerously-bypass-approvals-and-sandbox`` (``--yolo``)
  — sem prompt de aprovação, sem sandbox, essencial sem TTY.
* **Brief (§2.2):** prompt posicional (Codex não tem ``--message-file``). Conteúdo
  do brief é lido do arquivo. ``--skip-git-repo-check`` garante exec em workdir
  sem git ainda inicializado.
* **Modelo:** ``-m <model>``; ``None`` → default da config/conta.
* **Reasoning (§1.10):** ``-c model_reasoning_effort=<nivel>``; vocabulário oficial:
  ``minimal``/``low``/``medium``/``high``. ``supports_reasoning=True``.
* **Saída (§1.6):** ``--json`` → JSONL de eventos; :meth:`parse_output` lê o
  último evento de mensagem do agente. Exit-code grosso; gate de commit/push do
  server decide o sucesso final.
* **list_models:** catálogo estático curado (Codex não tem ``list-models``).
* **Resume (issue #445):** ``codex exec resume <thread_id>`` retoma transcript +
  plan + approvals sem re-gastar tokens. ``thread_id`` capturado do evento
  ``thread.started`` do JSONL (ver :meth:`extract_session_id`).
* **Auth (§1.11/§2.2):** DOIS modos. Default ``env`` — ``OPENAI_API_KEY`` (não
  expira). Opt-in ``oauth_file`` (``DEILE_CODEX_AUTH=oauth``) — ``auth.json``
  capturado via ``deploy.py k8s codex-login``. Seleção em runtime é do
  servidor/deploy, não do adapter.
* **Dirs graváveis (§1.7):** ``HOME`` + ``CODEX_HOME`` (config + auth.json OAuth).
* **Egress (§1.13):** ``api.openai.com``; forges adicionadas pela NetworkPolicy.
* **git (§1.5):** ``brief_driven`` — brief instrui git; server valida no gate.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

from .base import (BaseCliAdapter, ModelAuth, ModelInfo, OAuthSpec, ResumeCtx,
                   WorkResult, classify_provider_cutoff, iter_jsonl_events,
                   no_output_result, read_brief_or_fallback)

logger = logging.getLogger("deile.cli_adapters.codex")

#: Backup do ``auth.json`` OAuth preservado antes de ``codex login --with-api-key``.
#: Sem isso, trocar para API key destruiria a credencial OAuth e o próximo dispatch
#: de modelo ``chatgpt`` falharia (Frente 4 — "NÃO destrua as credenciais OAuth").
_OAUTH_BACKUP_NAME = "auth.oauth.json.bak"

_AUTH_FILE_NAME = "auth.json"

#: Runner de subprocess injetável (testes substituem para não chamar o binário
#: codex real). Assinatura compatível com ``subprocess.run``.
SubprocessRunner = Callable[..., "subprocess.CompletedProcess"]

#: Vocabulário oficial de reasoning effort do Codex. Valor fora do conjunto →
#: flag omitida (fail-open), não erro.
_VALID_REASONING = frozenset({"minimal", "low", "medium", "high"})

#: Catálogo estático curado (Codex não tem ``list-models`` confiável — §2.2).
#: IDs no formato nativo do ``-m`` (sem prefixo de provider; Codex assume OpenAI direto).
#:
#: **Auth POR MODELO** — validado empiricamente: modelos ``gpt-5*-codex`` premium
#: SÓ funcionam com conta ChatGPT (OAuth ``auth.json``) e são REJEITADOS via API
#: key (400 unsupported-model). ``gpt-5.1-codex-mini`` e ``codex-mini-latest``
#: aceitam ``OPENAI_API_KEY``. O ``cli_worker_server`` provisiona o auth antes de
#: invocar ``codex exec`` conforme este campo.
#:
#: Preços USD/1M tokens (input / cached / output), jun/2026. Ordem decrescente de capacidade.
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5.3-codex",
        label="GPT-5.3 Codex (ChatGPT)",
        provider="openai",
        price_in=1.75, cached_in=0.175, price_out=14.00,
        auth="chatgpt",
        notes="topo de linha; exige conta ChatGPT (OAuth)",
    ),
    ModelInfo(
        id="gpt-5.2-codex",
        label="GPT-5.2 Codex (ChatGPT)",
        provider="openai",
        price_in=1.75, cached_in=0.175, price_out=14.00,
        auth="chatgpt",
        notes="exige conta ChatGPT (OAuth)",
    ),
    ModelInfo(
        id="gpt-5.1-codex-max",
        label="GPT-5.1 Codex Max (ChatGPT)",
        provider="openai",
        price_in=1.25, cached_in=0.125, price_out=10.00,
        auth="chatgpt",
        notes="exige conta ChatGPT (OAuth)",
    ),
    ModelInfo(
        id="gpt-5.1-codex",
        label="GPT-5.1 Codex (ChatGPT)",
        provider="openai",
        price_in=1.25, cached_in=0.125, price_out=10.00,
        auth="chatgpt",
        notes="exige conta ChatGPT (OAuth)",
    ),
    ModelInfo(
        id="gpt-5-codex",
        label="GPT-5 Codex (ChatGPT)",
        provider="openai",
        price_in=1.25, cached_in=0.125, price_out=10.00,
        auth="chatgpt",
        notes="exige conta ChatGPT (OAuth); rejeitado via API key",
    ),
    ModelInfo(
        id="gpt-5.1-codex-mini",
        label="GPT-5.1 Codex Mini (API key)",
        provider="openai",
        price_in=0.25, cached_in=0.025, price_out=2.00,
        auth="apikey",
        notes="mais barato de coding; FUNCIONA via OPENAI_API_KEY",
    ),
    ModelInfo(
        id="codex-mini-latest",
        label="Codex Mini Latest (API key)",
        provider="openai",
        price_in=1.50, cached_in=0.375, price_out=6.00,
        auth="apikey",
        notes="aceita OPENAI_API_KEY",
    ),
]


class CodexAdapter(BaseCliAdapter):
    """Adapter do OpenAI Codex CLI (worker Tier 2 — OpenAI direto)."""

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
        """Monta o argv headless do ``codex exec``.

        Fresh: ``codex exec --cd <workdir> [-m <model>]
        [-c model_reasoning_effort=<r>] --dangerously-bypass-approvals-and-sandbox
        --json --skip-git-repo-check "<brief>"``.

        Resume (issue #445): ``codex exec resume <thread_id> [flags...] "<brief>"``
        — retoma transcript + plan + approvals sem re-gastar tokens. ``thread_id``
        vem do evento ``thread.started`` (ver :meth:`extract_session_id`). Forma
        confirmada na doc oficial (``codex exec resume <SESSION_ID>``).

        SEMPRE ``codex exec`` (nunca ``codex`` puro — panica sem TTY). Brief lido
        do arquivo como prompt posicional (Codex não tem ``--message-file``).
        """
        brief_text = read_brief_or_fallback(brief_path)
        argv: List[str] = ["codex", "exec"]
        if resume is not None and resume.session_id:
            argv += ["resume", resume.session_id]
            # ``--cd`` pertence ao ``codex exec`` fresh; ``codex exec resume <id>``
            # NÃO o aceita ("error: unexpected argument '--cd'"). No resume, o
            # cwd do subprocess (setado pelo server) já posiciona o codex.
        else:
            argv += ["--cd", workdir]
        if model:
            argv += ["-m", model]
        if reasoning and reasoning in _VALID_REASONING:
            argv += ["-c", f"model_reasoning_effort={reasoning}"]
        argv += [
            "--dangerously-bypass-approvals-and-sandbox",
            "--json",
            "--skip-git-repo-check",
            brief_text,
        ]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME + CODEX_HOME graváveis (config + auth.json OAuth).

        Não inclui ``auth_env_keys`` — ``OPENAI_API_KEY`` vem do Secret do Deployment.
        """
        return {
            "HOME": home,
            "CODEX_HOME": f"{home}/.codex",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o JSONL de ``--json`` num :class:`WorkResult`.

        Veredito = última mensagem de assistente (``agent_message``/``assistant``/
        ``message``). Eventos ``error`` → ``ok=False``. Exit-code é informativo
        apenas; gate de commit/push do server decide o sucesso final.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx/
        conexão) ANTES da heurística — retorna ``error_code`` específico, nunca
        "conclusão limpa", para o pipeline retomar o trabalho parcial.
        """
        if (cut := classify_provider_cutoff(stdout, stderr, "codex")):
            return cut

        last_text = ""
        error_text = ""
        saw_event = False

        for event in iter_jsonl_events(stdout):
            saw_event = True
            etype = self._event_type(event)
            if "error" in etype.lower():
                error_text = self._event_text(event) or error_text
                continue
            if any(k in etype.lower() for k in ("agent_message", "assistant", "message")):
                txt = self._event_text(event)
                if txt:
                    last_text = txt
                continue
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
            return WorkResult(
                ok=True,
                result_text="codex concluiu sem veredito textual explícito",
            )
        return no_output_result(stdout, stderr, rc, "codex")

    @staticmethod
    def _event_type(event: dict) -> str:
        """``type`` do evento, com fallback em ``msg.type``."""
        etype = event.get("type")
        if isinstance(etype, str) and etype:
            return etype
        msg = event.get("msg")
        if isinstance(msg, dict):
            inner = msg.get("type")
            if isinstance(inner, str):
                return inner
        return ""

    @staticmethod
    def _event_text(event: dict) -> str:
        """Extrai o texto de um evento JSONL, tolerante ao shape por versão.

        Inclui ``item`` (codex >=0.13x): ``{type:item.completed, item:{type:
        agent_message, text:"..."}}`` carrega o veredito do agente em
        ``item.text``. Sem isso o parser caía no fallback "sem veredito textual"
        mesmo com o codex tendo respondido (homologação E2E do stage follow_ups).
        """
        for key in ("message", "text", "content", "msg", "delta", "item"):
            val = event.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
            if isinstance(val, dict):
                nested = (
                    val.get("message")
                    or val.get("text")
                    or val.get("content")
                )
                if isinstance(nested, str) and nested.strip():
                    return nested.strip()
        return ""

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Extrai o ``thread.id`` do evento ``thread.started`` do JSONL.

        Com ``--json``, o primeiro evento é ``thread.started`` com ``thread.id``
        (ex.: ``thr_123``) — confirmado em ``exec_events.rs``. Esse id é o que
        ``codex exec resume <id>`` consome. Tolera ``thread_id``/``session_id``
        no topo para variações de versão. Vazio se nenhum evento de início emitido.
        """
        for event in iter_jsonl_events(stdout):
            thread = event.get("thread")
            if isinstance(thread, dict):
                tid = thread.get("id")
                if isinstance(tid, str) and tid.strip():
                    return tid.strip()
            for key in ("thread_id", "threadId", "session_id", "conversation_id"):
                val = event.get(key)
                if isinstance(val, str) and val.strip():
                    return val.strip()
        return ""

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático (Codex não tem ``list-models`` confiável)."""
        return list(_MODELS)

    @staticmethod
    def auth_for_model(model: Optional[str]) -> ModelAuth:
        """Modo de auth exigido por *model* (Frente 4).

        Modelo desconhecido ou ``None`` → ``"chatgpt"`` (conservador: a maioria
        dos modelos premium exige OAuth; melhor garantir do que falhar com 401).
        """
        if model:
            for m in _MODELS:
                if m.id == model and m.auth:
                    return m.auth
        return "chatgpt"

    def provision_auth(
        self,
        *,
        model: Optional[str],
        home: str,
        env: dict,
        runner: Optional[SubprocessRunner] = None,
    ) -> Tuple[bool, str]:
        """Garante o ``CODEX_HOME/auth.json`` no modo exigido por *model* (Frente 4).

        * ``apikey`` → backup do OAuth (se houver) + ``codex login --with-api-key``.
        * ``chatgpt`` → garante OAuth presente; restaura do backup se apikey sobrescreveu.

        Idempotente e não-destrutivo (nunca apaga OAuth — move para backup antes).
        ``ok=False`` aborta o dispatch.
        """
        run = runner or subprocess.run
        codex_home = Path(
            env.get("CODEX_HOME") or os.path.join(home, ".codex")
        )
        auth_path = codex_home / _AUTH_FILE_NAME
        backup_path = codex_home / _OAUTH_BACKUP_NAME
        mode = self.auth_for_model(model)
        try:
            codex_home.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return False, f"não criei CODEX_HOME {codex_home}: {exc}"

        if mode == "apikey":
            return self._provision_apikey(
                auth_path, backup_path, env=env, run=run,
            )
        return self._provision_chatgpt(auth_path, backup_path)

    @staticmethod
    def _provision_apikey(
        auth_path: Path, backup_path: Path, *, env: dict, run: SubprocessRunner,
    ) -> Tuple[bool, str]:
        """Modo API key: backup do OAuth + ``codex login --with-api-key``."""
        api_key = (env.get("OPENAI_API_KEY") or "").strip()
        if not api_key:
            return False, (
                "modelo exige API key mas OPENAI_API_KEY não está no env"
            )
        if auth_path.exists() and not backup_path.exists():
            if not CodexAdapter._looks_like_apikey_auth(auth_path):
                try:
                    shutil.copy2(auth_path, backup_path)
                except OSError as exc:
                    logger.warning("backup do auth.json OAuth falhou: %s", exc)
        try:
            result = run(
                ["codex", "login", "--with-api-key"],
                input=api_key,
                capture_output=True,
                text=True,
                timeout=30,
                env={**os.environ, **env},
            )
        except FileNotFoundError:
            return False, "binário codex não encontrado para login --with-api-key"
        except subprocess.TimeoutExpired:
            return False, "codex login --with-api-key expirou (30s)"
        except Exception as exc:  # noqa: BLE001 — login nunca derruba o dispatch sem msg
            return False, f"codex login --with-api-key falhou: {exc}"
        if getattr(result, "returncode", 1) != 0:
            tail = (getattr(result, "stderr", "") or
                    getattr(result, "stdout", "") or "")[-300:]
            return False, f"codex login --with-api-key rc!=0: {tail.strip()}"
        return True, "auth: API key (codex login --with-api-key)"

    @staticmethod
    def _provision_chatgpt(
        auth_path: Path, backup_path: Path,
    ) -> Tuple[bool, str]:
        """Modo ChatGPT: garante o ``auth.json`` OAuth (restaura do backup se apikey sobrescreveu)."""
        if backup_path.exists():
            try:
                shutil.copy2(backup_path, auth_path)
            except OSError as exc:
                return False, f"restore do auth.json OAuth falhou: {exc}"
            return True, "auth: OAuth ChatGPT (restaurado do backup)"
        if auth_path.exists():
            if CodexAdapter._looks_like_apikey_auth(auth_path):
                return False, (
                    "modelo exige conta ChatGPT (OAuth) mas só há credencial de "
                    "API key e nenhum backup OAuth — rode codex-login OAuth"
                )
            return True, "auth: OAuth ChatGPT (auth.json presente)"
        return False, (
            "modelo exige conta ChatGPT (OAuth) mas auth.json ausente — "
            "rode deploy.py k8s cli-worker-login (codex OAuth) antes"
        )

    @staticmethod
    def _looks_like_apikey_auth(auth_path: Path) -> bool:
        """Heurística: ``auth.json`` é de API key (não OAuth)?

        Codex grava ``OPENAI_API_KEY`` no auth.json do modo apikey; OAuth
        carrega ``tokens``/``refresh_token``. Em erro de parse assume OAuth
        (conservador — não sobrescreve o que não conseguiu ler).
        """
        try:
            data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        if not isinstance(data, dict):
            return False
        if data.get("OPENAI_API_KEY") and not data.get("tokens"):
            return True
        return False


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
#: ``auth_mode="env"`` (default; ``OPENAI_API_KEY``). ``oauth`` expõe o
#: :class:`OAuthSpec` opt-in (``DEILE_CODEX_AUTH=oauth``); seleção em runtime
#: é do servidor/deploy.
ADAPTER = CodexAdapter(
    kind="codex",
    default_port=8772,
    auth_mode="env",
    supports_resume=True,
    supports_reasoning=True,
    git_strategy="brief_driven",
    auth_env_keys=["OPENAI_API_KEY"],
    egress_hosts=["api.openai.com"],
    writable_dirs=["HOME", "CODEX_HOME"],
    oauth=OAuthSpec(
        cred_path="~/.codex/auth.json",
        login_cmd=["codex", "login", "--device-auth"],
        secret_name="codex-credentials",
        renewable=True,
    ),
)


__all__ = ["CodexAdapter", "ADAPTER"]
