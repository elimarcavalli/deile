#!/usr/bin/env python3
"""cli_adapters.base — contrato do adapter por CLI da frota multi-worker.

Cada CLI de coding headless (opencode, codex, qwen, aider, goose, antigravity,
claude) é plugado na frota por um **adapter** que implementa o
:class:`CliAdapter` Protocol. O servidor genérico (``cli_worker_server.py``)
reaproveita TODA a maquinaria agnóstica do ``_worker_core`` (lease, heartbeat,
subprocess one-shot, HTTP bearer, cleanup) e delega ao adapter apenas os **cinco
pontos** que de fato divergem entre CLIs:

1. ``build_argv``  — montar o argv headless (flags de autonomia, modelo, brief).
2. ``env_overlay`` — variáveis de ambiente que o CLI exige (HOME/XDG/config).
3. ``parse_output`` — interpretar stdout/stderr/rc → :class:`WorkResult`.
4. ``list_models`` — catálogo (estático ou dinâmico) que alimenta ``/v1/models``.
5. metadados de classe (``kind``, ``default_port``, ``auth_mode``, ...) que
   dirigem registro, painel, geração de manifests e NetworkPolicy.

**Single source of truth:** os metadados declarados aqui são lidos pelo
``dispatch_resolver`` (deriva ``VALID_DISPATCHERS``), pelo painel, pelo
``deploy.py gen-worker`` e pela geração de NetworkPolicy. Adicionar um worker =
escrever **um** adapter; nenhum consumidor é editado (eles iteram o registro).

Este módulo NÃO importa nada do CLI concreto nem toca rede/filesystem — é só o
contrato + dataclasses de transporte. O conteúdo plugável vive nos adapters
concretos (``cli_adapters/<kind>.py``), descobertos por auto-discovery no
``cli_adapters/__init__.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Literal, Optional, Protocol, runtime_checkable

#: Modo de autenticação suportado por um adapter.
#:
#: * ``env``        — chave de API via variável de ambiente (não expira;
#:                    caminho recomendado para automação).
#: * ``oauth_file`` — credencial OAuth montada num arquivo (claude/codex/
#:                    antigravity); permite usar assinatura, mas exige
#:                    bootstrap + refresh in-pod.
AuthMode = Literal["env", "oauth_file"]

#: Estratégia de commit/push do adapter.
#:
#: * ``cli_autocommit`` — o CLI commita sozinho (ex.: aider ``--auto-commits``);
#:                        o wrapper só faz push + valida.
#: * ``brief_driven``   — o brief instrui o agente a ``git add/commit/push``;
#:                        o wrapper valida que houve commit+push.
GitStrategy = Literal["cli_autocommit", "brief_driven"]


@dataclass(frozen=True)
class WorkResult:
    """Veredito de um dispatch, produzido por :meth:`CliAdapter.parse_output`.

    ``ok`` é a leitura que o adapter faz da saída do CLI. O servidor combina
    esse valor com um **gate pós-execução** (commit/push/testes) antes de
    declarar sucesso — exit-code dos CLIs não é confiável (ver plano §1.6).

    Attributes:
        ok: True se o adapter interpretou a saída como sucesso.
        result_text: resumo legível do resultado (veredito do agente, erro,
            ou tail relevante da saída). Vai para o campo ``result``/``error``
            da resposta HTTP.
        error_code: código estruturado de falha (ex.: ``WORKER_AUTH_EXPIRED``,
            ``NO_PUSH``) ou ``None`` em sucesso.
        cost_usd: custo do dispatch em USD quando o CLI o reporta; ``None``
            quando o CLI não expõe custo (a maioria — só claude/aider têm).
    """

    ok: bool
    result_text: str = ""
    error_code: Optional[str] = None
    cost_usd: Optional[float] = None


@dataclass(frozen=True)
class ResumeCtx:
    """Contexto de retomada de uma sessão anterior.

    Passado a :meth:`CliAdapter.build_argv` apenas quando o adapter declara
    ``supports_resume=True`` E o pipeline pediu resume. Adapters sem resume
    recebem ``None`` e sempre rodam fresh (o brief lê ``.deile-progress.md``
    para contexto natural).

    Attributes:
        session_id: identificador da sessão a retomar (semântica nativa do CLI).
        prev_task_id: task_id do dispatch anterior (hex 16) — localiza o
            workdir/metadata reaproveitados.
    """

    session_id: str
    prev_task_id: str


@dataclass(frozen=True)
class ModelInfo:
    """Um modelo suportado por um worker, exposto via ``GET /v1/models``.

    Alimenta o picker de modelo do painel (``DispatchMatrixView``) para o stage
    cujo worker é este. ``id`` é o model-id NATIVO do CLI (string livre, não o
    formato ``provider:model`` do deile-worker).

    Attributes:
        id: model-id nativo do CLI (ex.: ``openrouter/anthropic/claude-3.7-sonnet``,
            ``qwen3-coder-plus``, ``gpt-5.5-codex``).
        label: rótulo legível para o painel; default = ``id``.
        provider: provider de origem (``openrouter``, ``openai``, ...) ou ``None``.
        context: janela de contexto em tokens, quando conhecida.
        notes: observação curta (custo, caveat) ou ``None``.
    """

    id: str
    label: str = ""
    provider: Optional[str] = None
    context: Optional[int] = None
    notes: Optional[str] = None

    def as_dict(self) -> dict:
        """Serializa para o JSON de ``GET /v1/models`` (contrato §1.12)."""
        return {
            "id": self.id,
            "label": self.label or self.id,
            "provider": self.provider,
            "context": self.context,
            "notes": self.notes,
        }


@dataclass(frozen=True)
class OAuthSpec:
    """Especificação de OAuth de um adapter ``auth_mode="oauth_file"``.

    Generaliza o mecanismo do ``claude-login`` para qualquer CLI cujo login
    grava uma credencial em arquivo que pode ser capturada do host e montada
    no pod (claude, codex ChatGPT, antigravity Google).

    Attributes:
        cred_path: caminho do arquivo de credencial dentro do pod/host
            (ex.: ``~/.codex/auth.json``, ``~/.claude/credentials.json``).
        login_cmd: comando que o operador roda NO HOST para gerar a credencial
            (ex.: ``["codex", "login", "--device-auth"]``).
        secret_name: nome do K8s Secret que carrega a credencial capturada.
        renewable: True se há um caminho leve de renovação (``<kind>-renew``);
            False exige re-login completo.
    """

    cred_path: str
    login_cmd: List[str]
    secret_name: str
    renewable: bool = False


@runtime_checkable
class CliAdapter(Protocol):
    """Contrato de um adapter de CLI worker.

    Implementações concretas vivem em ``cli_adapters/<kind>.py`` e são
    instanciadas/descobertas pelo registro. O Protocol é ``runtime_checkable``
    para que o registro valide candidatos via ``isinstance(obj, CliAdapter)``
    sem herança nominal — basta ter os atributos/métodos.

    Os atributos de metadado são lidos como **dados** (declarados na instância)
    pelos consumidores (registro, painel, ``gen-worker``, NetworkPolicy); os
    métodos especializam o comportamento por dispatch.
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
    ) -> List[str]:
        """Monta o argv headless do CLI para um dispatch.

        Args:
            brief_path: caminho do arquivo com o brief (o servidor o escreve no
                workdir antes de chamar; adapters podem passá-lo via flag
                — opencode ``-f``, aider ``--message-file`` — ou ler o conteúdo).
            model: model-id nativo do CLI ou ``None`` (deixa o CLI decidir).
            reasoning: nível de reasoning já resolvido, ou ``None``; o adapter
                ignora se ``supports_reasoning=False``.
            workdir: diretório de trabalho do repositório (cwd do subprocess).
            resume: contexto de retomada ou ``None`` (sempre ``None`` se
                ``supports_resume=False``).

        Returns:
            Lista de tokens do argv para ``asyncio.create_subprocess_exec``.
        """
        ...

    def env_overlay(self, *, home: str) -> dict:
        """Variáveis de ambiente a sobrepor no env do subprocess.

        Inclui HOME/XDG/CONFIG-HOME apontando para dirs graváveis e qualquer
        config inline que o CLI consome por env (ex.: ``OPENCODE_CONFIG_CONTENT``,
        ``GOOSE_DISABLE_KEYRING``). NÃO inclui as ``auth_env_keys`` — essas vêm
        do Secret montado no Deployment.

        Args:
            home: HOME gravável do pod para este worker (ex.: ``/home/opencode``).

        Returns:
            Dict de ``{VAR: valor}`` mesclado sobre ``os.environ`` no spawn.
        """
        ...

    def parse_output(self, *, stdout: str, stderr: str, rc: int) -> WorkResult:
        """Interpreta a saída do subprocess num :class:`WorkResult`.

        Exit-code dos CLIs não é confiável (plano §1.6): o adapter decide ``ok``
        pela saída (JSON estruturado quando há, heurística senão), e o servidor
        ainda aplica o gate pós-run de commit/push/testes por cima.

        Args:
            stdout: saída padrão completa do subprocess.
            stderr: saída de erro completa do subprocess.
            rc: returncode do subprocess (124 = timeout, convenção do core).

        Returns:
            :class:`WorkResult` com ``ok``, ``result_text``, ``error_code``,
            ``cost_usd``.
        """
        ...

    def list_models(self) -> List[ModelInfo]:
        """Modelos suportados por este worker (alimenta ``GET /v1/models``).

        Pode ser um catálogo estático curado no adapter ou uma listagem
        dinâmica (rodar ``<cli> models`` e parsear). Quando dinâmico, o servidor
        cacheia o resultado (TTL) porque pode tocar a rede.

        Returns:
            Lista de :class:`ModelInfo`.
        """
        ...


@dataclass
class BaseCliAdapter:
    """Base opcional com defaults sensatos para adapters concretos.

    Reduz o boilerplate: um adapter herda daqui, define ``kind``/``default_port``
    e sobrescreve só o que diverge. Os metadados ganham defaults conservadores
    (sem resume, sem reasoning, ``env`` auth, ``brief_driven``); os métodos
    abstratos levantam ``NotImplementedError`` para falhar cedo se esquecidos.

    Não é obrigatória — qualquer objeto que satisfaça o Protocol :class:`CliAdapter`
    é aceito pelo registro. Existe só para conveniência e consistência.
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


__all__ = [
    "AuthMode",
    "GitStrategy",
    "WorkResult",
    "ResumeCtx",
    "ModelInfo",
    "OAuthSpec",
    "CliAdapter",
    "BaseCliAdapter",
]
