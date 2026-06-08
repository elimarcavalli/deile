#!/usr/bin/env python3
"""cli_adapters.codex — adapter do OpenAI Codex CLI (frota multi-worker, Tier 2).

Codex é o agente de coding headless da OpenAI (binário rust). Headless via
``codex exec`` (NUNCA ``codex`` puro — pode panicar sem TTY). Este adapter pluga
o Codex na frota pelos cinco pontos do contrato
:class:`~cli_adapters.base.CliAdapter`; toda a maquinaria genérica (lease,
heartbeat, subprocess, gate de git, HTTP) vem do ``cli_worker_server`` +
``_worker_core``.

Decisões deste adapter (alinhadas ao plano §2.2/§1.4/§1.6/§1.11 e validadas
contra a doc oficial do Codex via context7 — ``codex exec``, ``--json``,
``--skip-git-repo-check``, ``--sandbox``/approval-policy, ``CODEX_HOME``):

* **OpenAI DIRETO, não OpenRouter (§2.2):** Codex exige ``wire_api="responses"``
  no provider; a maioria dos modelos servidos pelo OpenRouter só fala Chat
  Completions → não funcionariam. Por isso este adapter assume OpenAI direto
  (``api.openai.com``); o catálogo de modelos lista os ``gpt-*`` premium.
* **Autonomia (§1.4):** ``--dangerously-bypass-approvals-and-sandbox`` (alias
  ``--yolo``) — sem prompt de aprovação, sem sandbox de rede, essencial sem TTY.
* **Brief (§2.2):** ``codex exec`` aceita o prompt como argumento posicional. O
  conteúdo do brief é lido do arquivo e passado como o prompt posicional (Codex
  não tem flag ``--message-file``). ``--skip-git-repo-check`` garante que o
  ``exec`` rode mesmo se o workdir ainda não for um repo git inicializado.
* **Modelo (§2.2):** ``-m <model>`` (ex.: ``gpt-5.5-codex``); ``None`` deixa o
  Codex usar o default da config/conta.
* **Reasoning (§1.10):** Codex suporta esforço de raciocínio (``-c
  model_reasoning_effort=<nivel>``); o vocabulário oficial é
  ``minimal``/``low``/``medium``/``high``. ``supports_reasoning=True``.
* **Saída (§1.6):** ``--json`` → JSONL de eventos; :meth:`parse_output` lê o
  último evento de mensagem do agente como veredito. Exit-code grosso → o gate
  de commit/push do server decide o sucesso final.
* **list_models:** Codex não tem comando de listagem confiável → **catálogo
  estático curado** (fonte: modelos ``gpt-*`` da OpenAI documentados).
* **Resume:** ``supports_resume=True`` (issue #445 — anti-sangria de custo).
  Codex tem ``resume`` nativo: quando o pipeline detecta trabalho começado, o
  worker retoma via ``codex exec resume <thread_id>`` (thread.id capturado do
  evento ``thread.started`` do JSONL) preservando transcript + plan + approvals,
  em vez de re-gastar tokens do zero. O brief continua lendo
  ``.deile-progress.md`` como contexto natural complementar.
* **Auth (§1.11/§2.2):** DOIS modos. **Default ``env``** — ``OPENAI_API_KEY``
  (não expira; robusto). **Opt-in ``oauth_file``** (``DEILE_CODEX_AUTH=oauth``)
  — ``codex login --device-auth`` no host grava ``auth.json`` sob ``CODEX_HOME``,
  capturado via ``deploy.py k8s codex-login`` (mesmo mecanismo do claude). Este
  adapter declara o modo ``env`` como metadado e expõe o :class:`OAuthSpec` em
  ``oauth`` para o caminho opt-in — a seleção em runtime é do servidor/deploy
  (``DEILE_CODEX_AUTH``), não do adapter.
* **Dirs graváveis (§1.7):** ``HOME`` + ``CODEX_HOME`` (config + auth.json no
  modo oauth) apontando para baixo de ``home``. O workdir do repo é gravável por
  construção.
* **Egress (§1.13):** ``api.openai.com``. As forges são adicionadas
  transversalmente pela geração de NetworkPolicy.
* **git (§1.5):** ``brief_driven`` — o brief instrui ``git add/commit/push`` sob
  auto-approve; o server valida commit novo + push no gate pós-run.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable, List, Optional, Tuple

import _worker_core as _core

from .base import (BaseCliAdapter, ModelAuth, ModelInfo, OAuthSpec, ResumeCtx,
                   WorkResult)

logger = logging.getLogger("deile.cli_adapters.codex")

#: Backup do ``auth.json`` OAuth (modo chatgpt) preservado quando o worker
#: troca para API key. Sem isso, ``codex login --with-api-key`` sobrescreveria
#: a credencial de assinatura e um dispatch seguinte de modelo ``chatgpt``
#: ficaria sem OAuth (Frente 4 — "NÃO destrua as credenciais OAuth").
_OAUTH_BACKUP_NAME = "auth.oauth.json.bak"

#: Nome do arquivo de credencial dentro de ``CODEX_HOME``.
_AUTH_FILE_NAME = "auth.json"

#: Runner de subprocess injetável (testes substituem para não chamar o binário
#: codex real). Assinatura compatível com ``subprocess.run``.
SubprocessRunner = Callable[..., "subprocess.CompletedProcess"]

#: Vocabulário oficial de reasoning effort do Codex (``model_reasoning_effort``).
#: Qualquer valor fora deste conjunto é silenciosamente ignorado (fail-open).
_VALID_REASONING = frozenset({"minimal", "low", "medium", "high"})

#: Catálogo estático curado (Codex não tem ``list-models`` confiável — §2.2).
#:
#: Fonte: modelos ``gpt-*-codex`` da OpenAI servidos via Responses API (o único
#: wire protocol que o Codex fala). IDs no formato nativo do ``-m`` do Codex,
#: sem prefixo de provider (Codex assume OpenAI direto).
#:
#: **Auth POR MODELO** (campo ``auth``) — fato verificado empiricamente + doc
#: OpenAI: os modelos ``gpt-5*-codex`` premium SÓ funcionam com conta ChatGPT
#: (OAuth ``auth.json``) e são REJEITADOS via API key com erro
#: "model is not supported when using Codex with a ChatGPT account" / 400
#: unsupported-model. Já ``gpt-5.1-codex-mini`` e ``codex-mini-latest`` aceitam
#: API key (``OPENAI_API_KEY``). O ``cli_worker_server`` provisiona o
#: ``CODEX_HOME/auth.json`` no modo exigido por este campo antes de invocar
#: ``codex exec`` (sem destruir a credencial OAuth — homes/backup separados).
#:
#: Preços em USD por 1M tokens (input / cached-input / output), tabela do
#: operador (jun/2026). Mantido em ordem decrescente de capacidade.
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

        Forma fresh: ``codex exec --cd <workdir> [-m <model>]
        [-c model_reasoning_effort=<r>] --dangerously-bypass-approvals-and-sandbox
        --json --skip-git-repo-check "<conteúdo do brief>"``.

        Forma resume (issue #445): ``codex exec resume <thread_id> --cd <workdir>
        [flags...] "<conteúdo do brief>"`` — o subcomando ``resume <id>`` retoma
        o thread anterior (transcript + plan + approvals preservados) e aplica o
        brief como nova instrução, em vez de re-gastar tokens do zero. Forma
        confirmada na doc oficial do Codex (``codex exec resume <SESSION_ID>``). O
        ``thread_id`` vem do evento ``thread.started`` do JSONL do dispatch
        anterior (capturado por :meth:`extract_session_id`).

        SEMPRE ``codex exec`` (nunca ``codex`` puro — panica sem TTY, §2.2). O
        brief é lido do arquivo e vira o prompt posicional (Codex não tem
        ``--message-file``).
        """
        brief_text = self._read_brief(brief_path)
        argv: List[str] = ["codex", "exec"]
        if resume is not None and resume.session_id:
            argv += ["resume", resume.session_id]
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

    @staticmethod
    def _read_brief(brief_path: str) -> str:
        """Lê o conteúdo do brief; em falha de I/O cai num prompt mínimo.

        O servidor escreve o brief no workdir antes de chamar; se por algum
        motivo o arquivo não puder ser lido, o prompt aponta o agente ao caminho
        para que ele mesmo o leia (degradação graciosa, sem estourar o build).
        """
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
        """Env do subprocess: HOME + CODEX_HOME graváveis.

        ``CODEX_HOME`` guarda config.toml e, no modo oauth, o ``auth.json`` (com
        refresh in-pod). NÃO inclui ``auth_env_keys`` (``OPENAI_API_KEY`` vem do
        Secret montado no Deployment quando ``auth_mode=env``).
        """
        return {
            "HOME": home,
            "CODEX_HOME": f"{home}/.codex",
        }

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Interpreta o JSONL de ``--json`` num :class:`WorkResult`.

        Codex emite uma linha JSON por evento. O veredito do agente é a última
        mensagem de assistente (eventos com ``type``/``msg.type`` indicando
        ``agent_message``/``assistant``/``message`` ou um campo textual). Eventos
        de erro (``type`` contendo ``error``) → ``ok=False``. Exit-code é
        informativo apenas (§1.6) — o gate de commit/push do server decide o
        sucesso final.

        Tolerante a linhas malformadas (parse best-effort). Sem nenhum evento
        textual e ``rc != 0`` → ``ok=False`` com tail do stderr.

        ANTI-SANGRIA (issue #445): classifica corte de provider (402/429/5xx/
        conexão) ANTES da heurística — retorna ``error_code`` específico em vez de
        "conclusão limpa" para o pipeline retomar o trabalho parcial.
        """
        provider_err = _core.classify_provider_error(f"{stdout}\n{stderr}")
        if provider_err:
            tail = (stderr or stdout)[-2000:].strip()
            return WorkResult(
                ok=False,
                result_text=tail or f"codex cortado por provider ({provider_err})",
                error_code=provider_err,
            )

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
            etype = self._event_type(event)
            if "error" in etype.lower():
                error_text = self._event_text(event) or error_text
                continue
            if any(k in etype.lower() for k in ("agent_message", "assistant", "message")):
                txt = self._event_text(event)
                if txt:
                    last_text = txt
                continue
            # Evento sem tipo reconhecível mas com texto solto.
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
        tail = (stderr or stdout)[-2000:].strip()
        return WorkResult(
            ok=False,
            result_text=tail or f"codex sem saída parseável (rc={rc})",
            error_code="NO_OUTPUT",
        )

    @staticmethod
    def _event_type(event: dict) -> str:
        """Extrai o ``type`` do evento, tolerante ao aninhamento em ``msg``."""
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

        Com ``--json``, o PRIMEIRO evento é ``thread.started`` carregando o
        ``thread`` (campo ``thread.id`` — ex.: ``thr_123``), confirmado no
        ``exec_events.rs`` do Codex. Esse id é o que ``codex exec resume <id>``
        consome. Tolera versões que aninham em ``thread_id``/``session_id`` no
        topo do evento. Vazio se nenhum evento de início foi emitido.
        """
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
        """Catálogo estático (Codex não tem ``list-models`` confiável — §2.2)."""
        return list(_MODELS)

    @staticmethod
    def auth_for_model(model: Optional[str]) -> ModelAuth:
        """Modo de auth exigido por *model* (Frente 4).

        Lê o campo ``ModelInfo.auth`` do catálogo. Modelo desconhecido ou
        ``None`` → ``"chatgpt"`` (conservador: a maioria dos modelos premium do
        Codex exige conta ChatGPT; melhor garantir o OAuth do que falhar com
        401 numa API key que não cobre aquele modelo).
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
        """Garante o ``CODEX_HOME/auth.json`` no modo exigido por *model*.

        Codex dual-mode por modelo (Frente 4):

        * ``apikey``  → faz backup do ``auth.json`` OAuth (se houver) e roda
          ``printenv OPENAI_API_KEY | codex login --with-api-key`` para gravar
          a credencial de API key. Exige ``OPENAI_API_KEY`` no env.
        * ``chatgpt`` → garante o ``auth.json`` OAuth presente; se o modo apikey
          o sobrescreveu antes, restaura do backup. A credencial OAuth original
          chega via initContainer (Secret/PVC) — esta função só a
          preserva/restaura, nunca a gera (login OAuth é do operador).

        Idempotente e não-destrutivo: nunca apaga a credencial OAuth (move para
        ``auth.oauth.json.bak`` antes de sobrescrever).

        Args:
            model: model-id selecionado (decide o modo via :meth:`auth_for_model`).
            home: HOME gravável (não usado direto — CODEX_HOME vem do env).
            env: env já com overlay (lê ``CODEX_HOME`` e ``OPENAI_API_KEY``).
            runner: injeção do subprocess runner (testes). Default ``subprocess.run``.

        Returns:
            ``(ok, detail)``; ``ok=False`` aborta o dispatch.
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
        # Preserva a credencial OAuth antes de sobrescrever (não-destrutivo).
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
        """Modo ChatGPT: garante o ``auth.json`` OAuth (restaura do backup)."""
        # Se o modo apikey sobrescreveu o auth.json, restaura o OAuth do backup.
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
        """Heurística: o ``auth.json`` é de API key (não OAuth)?

        Codex grava ``OPENAI_API_KEY`` no auth.json do modo API key; o OAuth
        carrega ``tokens``/``refresh_token``. Best-effort — em erro de parse
        assume OAuth (conservador: não sobrescreve o que não conseguiu ler).
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
#:
#: ``auth_mode="env"`` é o default recomendado (``OPENAI_API_KEY``, não expira).
#: ``oauth`` carrega o :class:`OAuthSpec` do caminho opt-in
#: (``DEILE_CODEX_AUTH=oauth`` → ``codex login --device-auth`` → ``auth.json``
#: capturado por ``deploy.py k8s codex-login``); a seleção em runtime é do
#: servidor/deploy, não do adapter.
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
