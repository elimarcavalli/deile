#!/usr/bin/env python3
"""cli_adapters.aider — adapter do Aider CLI (frota multi-worker, Tier 1).

Aider é o agente de coding pair-programming via terminal (pip). É o ÚNICO worker
da frota com ``git_strategy="cli_autocommit"``: o próprio Aider commita as
mudanças (``--auto-commits``); o wrapper só faz o ``push`` e (se o brief pediu)
roda os testes — o gate pós-run reflete isso. Headless via
``aider --message-file <brief> --yes-always``. Este adapter pluga o Aider na
frota pelos cinco pontos do contrato :class:`~cli_adapters.base.CliAdapter`; toda
a maquinaria genérica (lease, heartbeat, subprocess, gate de git, HTTP) vem do
``cli_worker_server`` + ``_worker_core``.

Decisões deste adapter (alinhadas ao plano §2.4/§1.4/§1.5/§1.11 e validadas
contra a doc oficial do Aider via context7 — ``--message-file``/``-f``,
``--yes-always``, ``--auto-commits``/``--no-auto-commits``,
``--attribute-*``/``--no-attribute-*``, ``--list-models``):

* **git ``cli_autocommit`` (§1.5/§2.4):** ``--auto-commits`` — o Aider commita.
  O wrapper detecta o commit local e só faz ``push``. O gate pós-run do server
  usa o ramo ``cli_autocommit``: há commit local → wrapper pusha + (se brief
  pediu) roda ``test_cmd``.
* **Atribuição (regra do projeto):** ``--no-attribute-author``,
  ``--no-attribute-committer`` e ``--no-attribute-commit-message-author`` para
  NÃO prefixar/marcar commits com "aider"/Co-Authored-By (regra global do
  Humano: sem Co-Authored-By, sem "(aider)").
* **Autonomia (§1.4):** ``--yes-always`` — confirma todos os prompts; sem TTY
  qualquer confirmação travaria o subprocess. ``--no-check-update`` e
  ``--analytics-disable`` evitam I/O de rede/prompt inesperado.
* **Single-pass (§2.4 gotcha):** ``--message-file --yes-always`` é uma passada
  só → pode commitar código quebrado. Por isso o gate pós-run de teste é
  OBRIGATÓRIO quando o brief exige suíte verde (responsabilidade do server, não
  do adapter). Aqui ``parse_output`` só lê a saída.
* **Brief (§2.4):** ``--message-file <brief_path>`` (flag oficial ``-f``) — o
  Aider lê o arquivo como a mensagem. Não há prompt posicional.
* **Modelo (§2.4):** ``--model <prov/model>`` (ex.:
  ``openrouter/anthropic/claude-3.7-sonnet``, ``deepseek/deepseek-chat``);
  ``None`` deixa o Aider usar o default da config/env. ``--weak-model`` barato
  para mensagens de commit fica como configuração de Deployment (não fixado aqui
  para não acoplar a um modelo específico).
* **Saída (§1.6):** Aider não tem ``--output-format json`` confiável headless →
  :meth:`parse_output` é heurística sobre stdout/stderr (detecta erros conhecidos
  e usa o tail como veredito). O gate de git/test do server é a autoridade final.
* **list_models:** DINÂMICO via ``aider --list-models ""`` (lista os modelos que
  o litellm conhece), com **catálogo curado de fallback** quando o comando
  falha/sem rede. O ``cli_worker_server`` cacheia com TTL.
* **Resume:** ``supports_resume=False`` (single-pass; o brief lê
  ``.deile-progress.md`` para contexto natural).
* **Auth (§1.11/§2.4):** ``env`` — ``OPENROUTER_API_KEY`` (uma chave → vários
  providers) ou ``DEEPSEEK_API_KEY``. Sem login, sem refresh.
* **Dirs graváveis (§1.7):** ``HOME`` (config/cache do litellm + history files do
  Aider). ``--no-gitignore`` evita o Aider mexer no ``.gitignore`` do repo. O
  workdir do repo é gravável por construção.
* **Egress (§1.13):** ``openrouter.ai`` + ``api.deepseek.com``. As forges são
  adicionadas transversalmente pela geração de NetworkPolicy.
"""

from __future__ import annotations

import logging
import shutil
import subprocess
from typing import List, Optional

from .base import BaseCliAdapter, ModelInfo, ResumeCtx, WorkResult

logger = logging.getLogger("deile.cli_adapters.aider")

#: Timeout (s) do ``aider --list-models`` em :meth:`list_models` (toca litellm).
_MODELS_CMD_TIMEOUT_S = 20

#: Trechos de stderr/stdout que sinalizam falha estrutural do Aider (heurística).
#: Conservador — só padrões inequívocos; o gate de git/test é a autoridade final.
_ERROR_MARKERS = (
    "litellm.exceptions",
    "authenticationerror",
    "ratelimiterror",
    "api key",
    "traceback (most recent call last)",
)

#: Catálogo curado de fallback quando ``aider --list-models`` falha/sem rede.
#:
#: Fonte: modelos OpenRouter/DeepSeek de uso recorrente na frota. IDs no formato
#: nativo do ``--model`` do Aider (litellm: ``provider/model``). A lista dinâmica
#: prevalece quando disponível; este catálogo só garante picker não-vazio.
_FALLBACK_MODELS: List[ModelInfo] = [
    ModelInfo(
        id="deepseek/deepseek-chat",
        label="DeepSeek Chat (direto)",
        provider="deepseek",
        notes="barato; default recomendado p/ o grosso do trabalho",
    ),
    ModelInfo(
        id="openrouter/anthropic/claude-3.7-sonnet",
        label="Claude 3.7 Sonnet (OpenRouter)",
        provider="openrouter",
        notes="premium; tarefas cirúrgicas críticas",
    ),
    ModelInfo(
        id="openrouter/deepseek/deepseek-chat",
        label="DeepSeek Chat (OpenRouter)",
        provider="openrouter",
    ),
    ModelInfo(
        id="openrouter/qwen/qwen3-coder",
        label="Qwen3 Coder (OpenRouter)",
        provider="openrouter",
    ),
]


class AiderAdapter(BaseCliAdapter):
    """Adapter do Aider CLI (worker Tier 1 — cirúrgico, auto-commit)."""

    def build_argv(
        self,
        *,
        brief_path: str,
        model: Optional[str],
        reasoning: Optional[str],
        workdir: str,
        resume: Optional[ResumeCtx],
    ) -> List[str]:
        """Monta o argv headless do ``aider``.

        Forma: ``aider [--model <m>] --message-file <brief_path> --yes-always
        --no-stream --no-pretty --analytics-disable --no-check-update
        --no-gitignore --auto-commits --no-attribute-author
        --no-attribute-committer --no-attribute-commit-message-author``.

        ``reasoning`` e ``resume`` são ignorados (sem suporte). O ``workdir`` é o
        cwd do subprocess (definido pelo core), não uma flag — o Aider opera no
        repo do diretório corrente.
        """
        argv: List[str] = ["aider"]
        if model:
            argv += ["--model", model]
        argv += [
            "--message-file", brief_path,
            "--yes-always",
            "--no-stream",
            "--no-pretty",
            "--analytics-disable",
            "--no-check-update",
            "--no-gitignore",
            "--auto-commits",
            # Regra do projeto: sem marca "aider"/Co-Authored-By nos commits.
            "--no-attribute-author",
            "--no-attribute-committer",
            "--no-attribute-commit-message-author",
        ]
        return argv

    def env_overlay(self, *, home: str) -> dict:
        """Env do subprocess: HOME gravável (config/cache litellm + history).

        NÃO inclui ``auth_env_keys`` (``OPENROUTER_API_KEY``/``DEEPSEEK_API_KEY``
        vêm do Secret montado no Deployment).
        """
        return {"HOME": home}

    def parse_output(
        self, *, stdout: str, stderr: str, rc: int,
    ) -> WorkResult:
        """Heurística sobre stdout/stderr (Aider não tem JSON headless confiável).

        Detecta marcadores de falha estrutural (auth/rate-limit/traceback) →
        ``ok=False``. Caso contrário, considera a execução plausível e usa o tail
        do stdout como veredito. Exit-code é informativo apenas (§1.6); o gate de
        git (commit local feito pelo Aider) + test do server decide o sucesso
        final (``git_strategy="cli_autocommit"``).
        """
        combined = f"{stdout}\n{stderr}".lower()
        for marker in _ERROR_MARKERS:
            if marker in combined:
                tail = (stderr or stdout)[-2000:].strip()
                return WorkResult(
                    ok=False,
                    result_text=tail or f"aider reportou erro (rc={rc})",
                    error_code="CLI_REPORTED_ERROR",
                )

        tail = (stdout or stderr).strip()
        if tail:
            return WorkResult(ok=True, result_text=tail[-2000:])
        # Sem saída alguma: não confia no rc, mas precisa de veredito; o gate de
        # commit/push do server confirma se o Aider de fato commitou.
        return WorkResult(
            ok=True,
            result_text="aider concluiu sem saída textual; gate de git confirma o commit",
        )

    def list_models(self) -> List[ModelInfo]:
        """Modelos suportados — dinâmico via ``aider --list-models``, com fallback.

        Roda ``aider --list-models ""`` (lista os modelos do litellm) e parseia.
        Se o binário não está no PATH, o comando falha, dá timeout ou não retorna
        linha válida → cai no :data:`_FALLBACK_MODELS` curado. O
        ``cli_worker_server`` cacheia o resultado (TTL) — pode tocar a rede.
        """
        dynamic = self._list_models_dynamic()
        return dynamic if dynamic else list(_FALLBACK_MODELS)

    @staticmethod
    def _list_models_dynamic() -> List[ModelInfo]:
        """Tenta listar via ``aider --list-models``; ``[]`` em qualquer falha."""
        if shutil.which("aider") is None:
            return []
        try:
            proc = subprocess.run(
                ["aider", "--list-models", ""],
                capture_output=True,
                text=True,
                timeout=_MODELS_CMD_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            logger.warning("`aider --list-models` falhou: %s", exc)
            return []

        models: List[ModelInfo] = []
        seen: set = set()
        for raw in proc.stdout.splitlines():
            line = raw.strip()
            # O Aider lista com prefixo "- " e linhas-cabeçalho; descarta ruído.
            if line.startswith("- "):
                line = line[2:].strip()
            if not line or "/" not in line or line in seen:
                continue
            if any(ch.isspace() for ch in line):
                continue
            # Linhas-cabeçalho do Aider terminam com ":" (ex.: "Models which match:")
            if line.endswith(":"):
                continue
            seen.add(line)
            provider = line.split("/", 1)[0]
            models.append(ModelInfo(id=line, provider=provider))
        return models


#: Instância exportada — descoberta pelo registro (``cli_adapters.ADAPTERS``).
ADAPTER = AiderAdapter(
    kind="aider",
    default_port=8774,
    auth_mode="env",
    supports_resume=False,
    supports_reasoning=False,
    git_strategy="cli_autocommit",
    auth_env_keys=["OPENROUTER_API_KEY", "DEEPSEEK_API_KEY"],
    egress_hosts=["openrouter.ai", "api.deepseek.com"],
    writable_dirs=["HOME"],
    oauth=None,
)


__all__ = ["AiderAdapter", "ADAPTER"]
