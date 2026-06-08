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
* **Resume:** ``supports_resume=False`` neste worker (fresh-only; o brief lê
  ``.deile-progress.md`` para contexto natural). Codex tem ``resume`` nativo, mas
  a frota padroniza fresh-com-contexto (espelha opencode) para não inflar estado.
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
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, OAuthSpec, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.codex")

#: Vocabulário oficial de reasoning effort do Codex (``model_reasoning_effort``).
#: Qualquer valor fora deste conjunto é silenciosamente ignorado (fail-open).
_VALID_REASONING = frozenset({"minimal", "low", "medium", "high"})

#: Catálogo estático curado (Codex não tem ``list-models`` confiável — §2.2).
#:
#: Fonte: modelos ``gpt-*`` da OpenAI servidos via Responses API (o único wire
#: protocol que o Codex fala). IDs no formato nativo do ``-m`` do Codex. Sem
#: prefixo de provider (Codex assume OpenAI direto). Mantido conservador — só os
#: modelos premium de coding que o operador escolheria conscientemente.
_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="gpt-5.5-codex",
        label="GPT-5.5 Codex (OpenAI)",
        provider="openai",
        notes="premium; melhor modelo de coding do Codex",
    ),
    ModelInfo(
        id="gpt-5.5",
        label="GPT-5.5 (OpenAI)",
        provider="openai",
        notes="premium; uso geral via Responses API",
    ),
    ModelInfo(
        id="gpt-5-codex",
        label="GPT-5 Codex (OpenAI)",
        provider="openai",
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
    ) -> List[str]:
        """Monta o argv headless do ``codex exec``.

        Forma: ``codex exec --cd <workdir> [-m <model>]
        [-c model_reasoning_effort=<r>] --dangerously-bypass-approvals-and-sandbox
        --json --skip-git-repo-check "<conteúdo do brief>"``.

        SEMPRE ``codex exec`` (nunca ``codex`` puro — panica sem TTY, §2.2). O
        brief é lido do arquivo e vira o prompt posicional (Codex não tem
        ``--message-file``). ``resume`` é ignorado (fresh-only).
        """
        brief_text = self._read_brief(brief_path)
        argv: List[str] = ["codex", "exec", "--cd", workdir]
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
        """
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

    def list_models(self) -> List[ModelInfo]:
        """Catálogo estático (Codex não tem ``list-models`` confiável — §2.2)."""
        return list(_MODELS)


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
    supports_resume=False,
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
