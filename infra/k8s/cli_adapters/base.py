#!/usr/bin/env python3
"""cli_adapters.base — contrato do adapter por CLI da frota multi-worker.

Cada CLI é plugado por um :class:`CliAdapter` que implementa cinco pontos:

1. ``build_argv``  — argv headless (flags de autonomia, modelo, brief).
2. ``env_overlay`` — vars de ambiente que o CLI exige (HOME/XDG/config).
3. ``parse_output`` — stdout/stderr/rc → :class:`WorkResult`.
4. ``list_models`` — catálogo que alimenta ``/v1/models``.
5. metadados (``kind``, ``default_port``, ``auth_mode``, ...) que dirigem
   registro, painel, ``deploy.py gen-worker`` e NetworkPolicy.

Single source of truth: metadados lidos pelo ``dispatch_resolver``, painel e
``gen-worker``. Adicionar worker = escrever um adapter; nenhum consumidor é
editado. Não importa nada de CLI concreto nem toca rede/filesystem.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import (Dict, Iterator, List, Literal, Optional, Protocol, Tuple,
                    runtime_checkable)

logger = logging.getLogger(__name__)


def read_brief_or_fallback(brief_path: str) -> str:
    """Lê o conteúdo do brief; em falha de I/O retorna prompt mínimo.

    Helper compartilhado pelos adapters que precisam materializar o brief
    como texto antes de passar ao CLI (qwen, goose, codex, antigravity).
    Em ``OSError`` retorna um prompt mínimo apontando para o arquivo —
    degradação graciosa, evita que IO transitório derrube o dispatch.
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


def classify_provider_cutoff(
    stdout: str, stderr: str, cli_name: str,
) -> Optional["WorkResult"]:
    """Prelude anti-sangria de ``parse_output`` — provider cutoff → ``WorkResult``.

    Chama ``_worker_core.classify_provider_error`` sobre stdout+stderr; se
    bater algum padrão (402/429/insufficient/…), devolve ``WorkResult(ok=False,
    error_code=<provider_err>, result_text=<tail or default>)``. Retorna
    ``None`` quando não há corte de provider — o adapter segue para o parse
    estruturado normal. Helper compartilhado por opencode/qwen/goose/codex/aider
    (todos com este mesmo trecho verbatim antes do extra-parsing).
    """
    # Import lazy (call-time, não no topo do módulo como faziam os adapters
    # originais): mover o helper para cá só é seguro porque ``_worker_core`` não
    # está em ``sys.path`` no import-time dos testes unitários dos adapters —
    # importar no topo de ``base.py`` quebraria a coleta. A resolução é
    # intencionalmente adiada para o momento da chamada.
    import _worker_core as _core
    provider_err = _core.classify_provider_error(f"{stdout}\n{stderr}")
    if not provider_err:
        return None
    tail = (stderr or stdout)[-2000:].strip()
    return WorkResult(
        ok=False,
        result_text=tail or f"{cli_name} cortado por provider ({provider_err})",
        error_code=provider_err,
    )


def iter_jsonl_events(text: str) -> Iterator[dict]:
    """Itera dicts JSONL em ``text``, tolerante a ruído fora do envelope JSON.

    Pula linhas vazias, linhas que não começam com ``{`` (logs Rust-style
    do CLI, banners, prompts), JSON malformado e payloads que não são dict.
    Helper compartilhado pelos adapters opencode/qwen/goose/codex que varrem
    NDJSON em ``parse_output`` e ``extract_session_id`` — antes, cada um
    repetia o mesmo loop com a guarda ``startswith('{')``; um esquecido
    estouraria ``json.loads`` em qualquer linha de log.
    """
    for line in text.splitlines():
        line = line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(event, dict):
            continue
        yield event


def no_output_result(
    stdout: str, stderr: str, rc: int, cli_name: str,
) -> "WorkResult":
    """Postlude ``NO_OUTPUT`` de ``parse_output`` — saída não-parseável.

    Devolve ``WorkResult(ok=False, error_code='NO_OUTPUT',
    result_text=<tail or default>)`` com tail de até 2000 chars do stderr/stdout.
    Helper compartilhado por opencode/qwen/goose/codex no fim do ``parse_output``
    quando nenhum branch estruturado casou.
    """
    tail = (stderr or stdout)[-2000:].strip()
    return WorkResult(
        ok=False,
        result_text=tail or f"{cli_name} sem saída parseável (rc={rc})",
        error_code="NO_OUTPUT",
    )

#: Modo de autenticação: ``env`` = API key (não expira; automação);
#: ``oauth_file`` = credencial OAuth em arquivo (claude/codex/antigravity;
#: exige bootstrap + refresh in-pod).
AuthMode = Literal["env", "oauth_file"]

#: Estratégia de commit/push: ``cli_autocommit`` = CLI commita sozinho (aider
#: ``--auto-commits``); ``brief_driven`` = brief instrui o agente a commitar.
GitStrategy = Literal["cli_autocommit", "brief_driven"]


@dataclass(frozen=True)
class WorkResult:
    """Veredito de um dispatch, produzido por :meth:`CliAdapter.parse_output`.

    ``ok`` é a leitura do adapter; o servidor ainda aplica gate pós-execução
    (commit/push/testes) antes de declarar sucesso — exit-code não é confiável
    (§1.6). ``cost_usd`` só presente para claude/aider.

    Observabilidade de custo central (issue #638) — campos estruturados de uso,
    preenchidos pelo servidor a partir do parser único ``fleet_progress_parse``
    (não pelo adapter, que tem múltiplos pontos de retorno). Viajam na resposta
    do ``/v1/dispatch`` para o pipeline persistir 1 registro por modelo no
    ``UsageRepository`` central (independe de pod/PVC):

    * ``tokens_by_model`` — ``{model_id: {in,out,cache_read,cache_write}}``;
      um dispatch multi-modelo produz N entradas → N registros centrais.
    * ``model`` — model-id predominante do dispatch (anti ``unknown``: cai no
      ``cli_model`` do payload quando o CLI não emite modelo no stdout).
    """

    ok: bool
    result_text: str = ""
    error_code: Optional[str] = None
    cost_usd: Optional[float] = None
    tokens_by_model: Dict[str, Dict[str, int]] = field(default_factory=dict)
    model: Optional[str] = None


@dataclass(frozen=True)
class ResumeCtx:
    """Contexto de retomada passado a :meth:`CliAdapter.build_argv` (issue #445).

    Passado apenas quando ``supports_resume=True`` E o pipeline pediu resume.
    Retomada nativa por CLI em vez de re-gastar tokens do zero:

    * **opencode**: ``--session <id>`` (sessionID no NDJSON).
    * **codex**: ``codex exec resume <thread_id>`` (thread.started no JSONL).
    * **qwen**: ``--resume <session_id>`` (session_id nos eventos JSON).
    * **goose**: ``goose run --name <task_id> --resume`` (SQLite nomeado;
      session_id = task_id determinístico).
    * **aider**: ``--restore-chat-history`` (keyed-by-workdir; session_id
      sentinela = task_id).
    """

    session_id: str
    prev_task_id: str


#: Auth exigida POR MODELO (codex dual-mode): ``apikey`` = API key via env;
#: ``chatgpt`` = OAuth ``auth.json`` (rejeita API key); ``None`` = usa
#: ``auth_mode`` do adapter. A escolha do modelo dita qual credencial provisionar.
ModelAuth = Literal["apikey", "chatgpt"]


@dataclass(frozen=True)
class ModelInfo:
    """Modelo suportado por um worker, exposto via ``GET /v1/models``.

    ``id`` é o model-id NATIVO do CLI (string livre, não ``provider:model``).
    ``price_*`` e ``auth`` são opcionais/retrocompat: quando presentes, o painel
    exibe custo e modo de auth, e o worker deriva qual credencial provisionar.
    """

    id: str
    label: str = ""
    provider: Optional[str] = None
    context: Optional[int] = None
    notes: Optional[str] = None
    price_in: Optional[float] = None
    price_out: Optional[float] = None
    cached_in: Optional[float] = None
    auth: Optional[ModelAuth] = None

    def as_dict(self) -> dict:
        """Serializa para o JSON de ``GET /v1/models`` (contrato §1.12)."""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "provider": self.provider,
            "context": self.context,
            "notes": self.notes,
            "price_in": self.price_in,
            "price_out": self.price_out,
            "cached_in": self.cached_in,
            "auth": self.auth,
        }


@dataclass(frozen=True)
class OAuthSpec:
    """OAuth spec de adapters ``auth_mode="oauth_file"``.

    Generaliza o mecanismo ``claude-login`` para CLIs cujo login grava uma
    credencial em arquivo (claude, codex ChatGPT, antigravity Google):
    captura do host → K8s Secret → montado no pod. ``renewable=False``
    exige re-login completo; ``True`` permite renovação leve (``<kind>-renew``).
    """

    cred_path: str
    login_cmd: List[str]
    secret_name: str
    renewable: bool = False


@runtime_checkable
class CliAdapter(Protocol):
    """Contrato de um adapter de CLI worker.

    ``runtime_checkable`` para que o registro valide via ``isinstance`` sem
    herança nominal. Atributos de metadado são lidos como dados pelos
    consumidores (registro, painel, ``gen-worker``, NetworkPolicy).
    """

    # ---- metadados (single source of truth p/ registro/painel/manifests) ----
    kind: str
    default_port: int
    auth_mode: AuthMode
    supports_resume: bool
    supports_reasoning: bool
    git_strategy: GitStrategy
    auth_env_keys: List[str]
    egress_hosts: List[str]
    writable_dirs: List[str]
    oauth: Optional[OAuthSpec]

    # ---- comportamento (especialização por dispatch) ----
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
        """Monta o argv headless do CLI para um dispatch.

        ``task_id`` (hex 16, default ``""`` para retrocompat): adapters cujo
        resume é keyed por nome determinístico (goose ``--name``) usam-no para
        que fresh e resume compartilhem a mesma sessão; demais ignoram.
        """
        ...

    def env_overlay(self, *, home: str) -> dict:
        """Vars de ambiente a sobrepor no env do subprocess.

        Inclui HOME/XDG/config e vars inline do CLI (ex.: ``OPENCODE_CONFIG_CONTENT``,
        ``GOOSE_DISABLE_KEYRING``). NÃO inclui ``auth_env_keys`` — essas vêm
        do Secret montado no Deployment.
        """
        ...

    def parse_output(self, *, stdout: str, stderr: str, rc: int) -> WorkResult:
        """Interpreta a saída do subprocess num :class:`WorkResult`.

        Exit-code não é confiável (§1.6): decide ``ok`` pela saída (JSON
        estruturado quando há, heurística senão). ``rc=124`` = timeout.
        """
        ...

    def list_models(self) -> List[ModelInfo]:
        """Modelos suportados (alimenta ``GET /v1/models``).

        Pode ser catálogo estático ou dinâmico (``<cli> models``). Quando
        dinâmico, o servidor cacheia (TTL) porque pode tocar rede.
        """
        ...

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Extrai o session-id nativo da saída para persistir no meta da task.

        Persiste em ``.sessions/<task_id>.json``; viaja de volta via
        ``resume-info`` para que o adapter retome a conversa nativa (issue #445).
        CLIs workdir-keyed (goose, aider) retornam ``task_id`` sentinela.
        Retorna ``""`` quando o adapter não suporta resume (server não persiste).
        """
        ...

    def provision_auth(
        self, *, model: Optional[str], home: str, env: dict,
    ) -> "Tuple[bool, str]":
        """Garante a credencial certa antes de invocar o CLI (dual-mode opt-in).

        Default = no-op (auth via env não muda por modelo). O codex sobrescreve:
        ``ModelInfo.auth`` dita se provisionamos OAuth ``auth.json`` ou API key.
        Implementações NÃO podem destruir credencial OAuth ao trocar para API key
        (backup/restore). ``ok=False`` aborta o dispatch com erro tipado.
        """
        ...


@dataclass
class BaseCliAdapter:
    """Base opcional com defaults para adapters concretos.

    Herdar, definir ``kind``/``default_port`` e sobrescrever só o que diverge.
    Defaults conservadores: sem resume/reasoning, ``env`` auth, ``brief_driven``.
    Não obrigatória — qualquer objeto que satisfaça :class:`CliAdapter` é aceito.
    """

    kind: str = ""
    default_port: int = 0
    auth_mode: AuthMode = "env"
    supports_resume: bool = False
    supports_reasoning: bool = False
    git_strategy: GitStrategy = "brief_driven"
    auth_env_keys: List[str] = field(default_factory=list)
    egress_hosts: List[str] = field(default_factory=list)
    writable_dirs: List[str] = field(default_factory=list)
    oauth: Optional[OAuthSpec] = None

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
        raise NotImplementedError(
            f"adapter {self.kind!r} must implement build_argv"
        )

    def env_overlay(self, *, home: str) -> dict:
        return {}

    def parse_output(self, *, stdout: str, stderr: str, rc: int) -> WorkResult:
        raise NotImplementedError(
            f"adapter {self.kind!r} must implement parse_output"
        )

    def list_models(self) -> List[ModelInfo]:
        return []

    def extract_session_id(
        self, *, stdout: str, stderr: str, task_id: str,
    ) -> str:
        """Sem resume → ``""`` (o server não persiste id vazio).

        Adapters com ``supports_resume=True`` sobrescrevem para extrair o
        session-id nativo (ou retornar ``task_id`` sentinela se keyed-by-workdir).
        """
        return ""

    def provision_auth(
        self, *, model: Optional[str], home: str, env: dict,
    ) -> Tuple[bool, str]:
        """No-op por default — auth via env não muda por modelo."""
        return True, ""


__all__ = [
    "AuthMode",
    "ModelAuth",
    "GitStrategy",
    "WorkResult",
    "ResumeCtx",
    "ModelInfo",
    "OAuthSpec",
    "CliAdapter",
    "BaseCliAdapter",
    "read_brief_or_fallback",
    "classify_provider_cutoff",
    "no_output_result",
    "iter_jsonl_events",
]
