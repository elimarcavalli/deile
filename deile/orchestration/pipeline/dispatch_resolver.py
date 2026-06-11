"""Dispatch resolver — espelha :mod:`model_resolver` mas para a escolha de
worker (qual pod recebe o POST /v1/dispatch) ao invés de modelo.

Cada stage do pipeline (``classify``, ``refine``, ``implement``, ``pr_review``,
``follow_ups``) pode ter seu dispatcher overriden via env var ou
``~/.deile/settings.json``; sem override, cai pro global
``DEILE_PIPELINE_DISPATCH_MODE`` / ``pipeline.dispatch_mode``; sem isso,
default built-in é ``deile-worker``.

A escolha entre os dois é independente da escolha do modelo (issue #309
correção do user: worker ≠ modelo). ``claude-worker`` só aceita modelos
``anthropic:*``; ``deile-worker`` aceita qualquer modelo.

Precedência de ``resolve_stage_dispatcher`` (alta → baixa):

1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var — vence tudo (retrocompat +
   override emergencial de cluster). Valor inválido → ValueError fail-fast.
2. ``pipeline.dispatchers.<stage>`` no settings.json layered. Valor inválido
   → warning + fallback (erro de usuário, não de operador).
3. ``DEILE_PIPELINE_DISPATCH_MODE`` env var (global). Valor inválido →
   ValueError fail-fast.
4. ``pipeline.dispatch_mode`` no settings.json layered. Valor inválido →
   warning + fallback.
5. Built-in default: ``deile-worker``.
"""
from __future__ import annotations

import logging
import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, FrozenSet, Optional, Tuple

logger = logging.getLogger(__name__)

# Re-export — :data:`PIPELINE_STAGES` vive em ``model_resolver`` (decisão #41,
# o módulo mais antigo dos per-stage resolvers). Mantemos o nome aqui para
# preservar a API pública usada por consumers como
# :mod:`deile.infrastructure.deile_worker_client` que importam de
# ``dispatch_resolver``; a tupla é a MESMA instância (não cópia) — testes
# podem checar identidade.
from deile.orchestration.pipeline.model_resolver import \
    PIPELINE_STAGES  # noqa: E402, F401

# ---------------------------------------------------------------------------
# Frota escalável — workers derivados do registro de adapters (issue multi-CLI)
# ---------------------------------------------------------------------------
#
# Os dois workers "núcleo" (``deile-worker`` in-process e ``claude-worker`` com
# OAuth) têm servers dedicados e existem independentemente da frota de CLIs. Os
# demais workers (``opencode-worker``, ``codex-worker``, ...) são plugados pelo
# **registro de adapters** ``cli_adapters.ADAPTERS`` (``infra/k8s/cli_adapters/``):
# cada adapter declara ``kind`` + ``default_port`` e, ao ser descoberto, vira um
# dispatcher válido ``<kind>-worker`` SEM editar este módulo.
#
# **Single source of truth (anti-hardcode):** ``VALID_DISPATCHERS``, os aliases e
# os endpoints são DERIVADOS do registro a cada resolução — nunca uma lista
# fixa. O teste de regressão ``test_worker_registry_drives_everything.py`` falha
# se alguém re-hardcodar uma lista de workers em qualquer consumidor.

#: Workers núcleo — têm server dedicado (deile-worker in-process; claude-worker
#: com OAuth). Existem fora do registro de adapters da frota CLI.
BUILTIN_DISPATCHERS: FrozenSet[str] = frozenset({"deile-worker", "claude-worker"})

#: Aliases legacy de PR #330 — as formas underscore/abreviadas que um operador
#: pode ter em ``DEILE_PIPELINE_DISPATCH_MODE``. Estes dois frozensets são a
#: **fonte única** do vocabulário de aliases dos workers núcleo; o
#: :mod:`deile.orchestration.pipeline.implementer` os importa daqui (antes cada
#: módulo mantinha sua própria cópia "em paridade" — um hazard de drift manual).
WORKER_ALIASES: FrozenSet[str] = frozenset(
    {"deile_worker", "worker", "deile", "deile-worker"}
)
CLAUDE_ALIASES: FrozenSet[str] = frozenset({"claude", "claude_code", "claude-code"})

#: Mapa alias→canônico dos workers núcleo, DERIVADO dos frozensets acima
#: (``claude-worker`` mapeia para si mesmo, espelhando ``deile-worker`` que já
#: vive em ``WORKER_ALIASES``). Derivar evita re-listar os mesmos aliases.
_BUILTIN_DISPATCHER_ALIASES: Dict[str, str] = {
    **{alias: "deile-worker" for alias in WORKER_ALIASES},
    **{alias: "claude-worker" for alias in CLAUDE_ALIASES | {"claude-worker"}},
}

#: Default endpoints dos workers núcleo. Env vars sobrescrevem (dev local).
_BUILTIN_ENDPOINT_DEFAULTS: Dict[str, str] = {
    "deile-worker": "http://deile-worker:8766",
    "claude-worker": "http://claude-worker:8767",
}
_BUILTIN_ENDPOINT_ENV_VARS: Dict[str, str] = {
    "deile-worker": "DEILE_WORKER_ENDPOINT",
    "claude-worker": "DEILE_CLAUDE_WORKER_ENDPOINT",
}

_DEFAULT_DISPATCHER = "deile-worker"


def _load_cli_adapter_registry() -> Dict[str, object]:
    """Importa ``cli_adapters.ADAPTERS`` de forma robusta; ``{}`` se ausente.

    O pacote ``cli_adapters`` vive em ``infra/k8s/`` (fora do pacote ``deile``):
    no cluster ele é COPiado plano para ``/app/cli_adapters`` (WORKDIR ``/app``,
    já em ``sys.path``); em dev/local insere-se ``infra/k8s`` no path. Quando o
    pacote não está disponível (ex.: instalação só do ``deile`` sem a frota CLI),
    a função degrada para ``{}`` — os dois workers núcleo continuam válidos.

    Tolerante a falha por desenho: um erro de import NUNCA derruba o resolver
    (o pipeline tem de resolver ao menos ``deile-worker``/``claude-worker``).
    """
    try:
        import cli_adapters  # noqa: PLC0415 — import lazy (pacote opcional fora de deile/)
    except ImportError:
        # Tenta inserir ``infra/k8s`` no path (layout de repo/dev) e re-importar.
        repo_root = Path(__file__).resolve().parents[3]
        infra_k8s = repo_root / "infra" / "k8s"
        if infra_k8s.is_dir() and str(infra_k8s) not in sys.path:
            sys.path.insert(0, str(infra_k8s))
        try:
            import cli_adapters  # noqa: PLC0415
        except Exception as exc:  # noqa: BLE001 — frota CLI é opcional; degrada
            logger.debug("cli_adapters indisponível (%s) — só workers núcleo", exc)
            return {}
    except Exception as exc:  # noqa: BLE001 — adapter quebrado não derruba o resolver
        logger.warning("falha ao carregar cli_adapters (%s) — só workers núcleo", exc)
        return {}
    return dict(getattr(cli_adapters, "ADAPTERS", {}) or {})


def _cli_dispatcher_ports() -> Dict[str, int]:
    """Mapa ``{<kind>-worker: default_port}`` derivado do registro de adapters.

    Cada adapter contribui um dispatcher ``<kind>-worker``; um ``kind`` que
    colida com um worker núcleo (``deile``/``claude``) é ignorado (o núcleo
    prevalece). Adapter sem ``default_port`` plausível (> 0) é pulado com
    warning — não dá pra montar endpoint sem porta.
    """
    ports: Dict[str, int] = {}
    for kind, adapter in _load_cli_adapter_registry().items():
        if not kind or not isinstance(kind, str):
            continue
        dispatcher = f"{kind}-worker"
        if dispatcher in BUILTIN_DISPATCHERS:
            # Núcleo prevalece — não deixa um adapter sequestrar deile/claude.
            continue
        port = getattr(adapter, "default_port", 0)
        if not isinstance(port, int) or port <= 0:
            logger.warning(
                "adapter %r sem default_port válido (%r) — dispatcher %r ignorado",
                kind, port, dispatcher,
            )
            continue
        ports[dispatcher] = port
    return ports


def get_valid_dispatchers() -> FrozenSet[str]:
    """Conjunto de dispatchers válidos = núcleo ∪ frota do registro de adapters.

    Re-derivado a cada chamada para refletir adapters registrados em runtime
    (hot-reload em testes via ``cli_adapters.reload_adapters()``). É a fonte
    única — qualquer consumidor que precise da lista de workers deve chamar
    esta função em vez de hardcodar.
    """
    return BUILTIN_DISPATCHERS | frozenset(_cli_dispatcher_ports())


def _dispatcher_aliases() -> Dict[str, str]:
    """Mapa alias→canônico = aliases dos workers núcleo + identidade da frota.

    Cada CLI dispatcher ``<kind>-worker`` aceita tanto a forma canônica quanto a
    forma curta ``<kind>`` (paridade com os aliases ``deile``/``claude`` do
    núcleo). Derivado a cada chamada do registro.
    """
    aliases = dict(_BUILTIN_DISPATCHER_ALIASES)
    for dispatcher in _cli_dispatcher_ports():
        aliases[dispatcher] = dispatcher
        short = dispatcher[: -len("-worker")]
        # Não sobrescreve um alias de núcleo já mapeado (defensivo).
        aliases.setdefault(short, dispatcher)
    return aliases


def _endpoint_for_dispatcher(canonical: str) -> Tuple[str, Optional[str]]:
    """Retorna ``(default_url, env_var)`` para um dispatcher canônico.

    Workers núcleo usam os endpoints fixos + env vars dedicadas; workers da
    frota CLI derivam ``http://<kind>-worker:<default_port>`` do adapter e a env
    var ``DEILE_<KIND>_WORKER_ENDPOINT``.
    """
    if canonical in _BUILTIN_ENDPOINT_DEFAULTS:
        return _BUILTIN_ENDPOINT_DEFAULTS[canonical], _BUILTIN_ENDPOINT_ENV_VARS[canonical]
    ports = _cli_dispatcher_ports()
    port = ports.get(canonical)
    if port is None:
        raise ValueError(f"unknown dispatcher {canonical!r}")
    kind = canonical[: -len("-worker")]
    env_var = f"DEILE_{kind.upper().replace('-', '_')}_WORKER_ENDPOINT"
    return f"http://{canonical}:{port}", env_var


#: Snapshot de import dos dispatchers válidos. Mantido por compat com
#: importadores que esperam uma constante (ex.: testes que checam pertinência).
#: Para a verdade em runtime — que reflete adapters hot-reloaded — use
#: :func:`get_valid_dispatchers`. O snapshot reflete a frota descoberta no
#: import do módulo (núcleo + adapters já presentes no pacote).
VALID_DISPATCHERS: FrozenSet[str] = get_valid_dispatchers()

#: Snapshot de import do mapa alias→canônico. Mantido por compat com
#: importadores externos (``_panel_data.py`` lê ``_DISPATCHER_ALIASES`` lazy).
#: A verdade em runtime vem de :func:`_dispatcher_aliases` (re-derivada).
_DISPATCHER_ALIASES: Dict[str, str] = _dispatcher_aliases()

#: Built-in timeout defaults (seconds) when no per-stage or global override is set.
#: claude-worker runs ``claude -p`` subprocesses that take longer; deile-worker
#: is in-process and faster. Mirrors ``pipeline_claude_timeout`` (1800) and
#: the new ``pipeline_deile_timeout`` (900) defaults in Settings.
BUILT_IN_TIMEOUT_S_CLAUDE: int = 1800
BUILT_IN_TIMEOUT_S_DEILE: int = 900

#: Built-in max retries default — formerly hard-coded in the monitor loop.
#: Extracted here (issue #391) so it can be overridden per-stage or globally.
BUILT_IN_MAX_RETRIES: int = 3


def is_valid_dispatcher(value: Optional[str]) -> bool:
    """Returns True se *value* é dispatcher válido (canônico OU legacy alias).

    Case-insensitive; whitespace stripped. Falsy / não-string → False. Os
    workers da frota CLI (``<kind>-worker`` + forma curta ``<kind>``) são
    aceitos automaticamente assim que o adapter é registrado.
    """
    if not value or not isinstance(value, str):
        return False
    return value.strip().lower() in _dispatcher_aliases()


def _canonicalize(value: Optional[str]) -> Optional[str]:
    """Normaliza para forma canônica em :func:`get_valid_dispatchers`; None se vazio.

    Aceita aliases legacy (PR #330) + a frota CLI do registro de adapters e
    canonicaliza para a forma ``<x>-worker``. Valor não reconhecido → ValueError
    fail-fast (típico typo).
    """
    if not value or not value.strip():
        return None
    normalized = value.strip().lower()
    aliases = _dispatcher_aliases()
    canonical = aliases.get(normalized)
    if canonical is None:
        valid = get_valid_dispatchers()
        raise ValueError(
            f"unknown dispatcher {value!r}; expected one of "
            f"{sorted(valid)} (or aliases "
            f"{sorted(set(aliases) - valid)})"
        )
    return canonical


def _canonicalize_settings(value: Optional[str], context: str) -> Optional[str]:
    """Like ``_canonicalize`` but logs a warning instead of raising on invalid.

    Used for settings.json values: a user typo should fall through to the next
    precedence level rather than crashing the pipeline with a ValueError.
    """
    if not value or not value.strip():
        return None
    try:
        return _canonicalize(value)
    except ValueError:
        logger.warning(
            "dispatch_resolver: invalid dispatcher %r in settings.json (%s); "
            "ignoring — expected one of %s",
            value,
            context,
            sorted(get_valid_dispatchers()),
        )
        return None


def resolve_stage_dispatcher(stage: str) -> str:
    """Resolve qual dispatcher (worker pod) recebe o dispatch de *stage*.

    Fallback chain (top → bottom):

    1. ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.dispatchers.<stage>`` in settings.json — warn + skip on invalid.
    3. ``DEILE_PIPELINE_DISPATCH_MODE`` env var — fail-fast on invalid.
    4. ``pipeline.dispatch_mode`` in settings.json — warn + skip on invalid.
    5. Built-in default: ``deile-worker``.

    Raises:
        ValueError: stage não está em :data:`PIPELINE_STAGES` (programming bug,
            não user input — implementer methods passam de uma whitelist).
        ValueError: env var contém valor não-whitelisted (fail-fast para evitar
            queimar budget no engine errado por typo).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    # 1. Env var per-stage (fail-fast: ops config errors must surface loud)
    stage_env = os.environ.get(f"DEILE_PIPELINE_DISPATCH_{stage.upper()}")
    resolved = _canonicalize(stage_env)
    if resolved:
        return resolved

    # 2. Settings per-stage (graceful: user config errors fall through with warning)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_per_stage = getattr(settings, f"pipeline_dispatcher_{stage}", None)
    resolved = _canonicalize_settings(
        settings_per_stage, f"pipeline.dispatchers.{stage}"
    )
    if resolved:
        return resolved

    # 3. Env var global (fail-fast)
    global_env = os.environ.get("DEILE_PIPELINE_DISPATCH_MODE")
    resolved = _canonicalize(global_env)
    if resolved:
        return resolved

    # 4. Settings global (graceful); default "deile_worker" canonicalizes to
    #    "deile-worker", so this step also covers the built-in default (step 5).
    resolved = _canonicalize_settings(
        settings.pipeline_dispatch_mode, "pipeline.dispatch_mode"
    )
    if resolved:
        return resolved

    # 5. Hardcoded safety net (only reached if settings.pipeline_dispatch_mode
    #    is empty or an unrecognized alias — extremely unlikely in practice).
    return _DEFAULT_DISPATCHER


def _parse_stage_int_env(env_name: str, *, min_value: int) -> Optional[int]:
    """Parse a per-stage integer env var, or ``None`` when unset/empty.

    Shared by :func:`resolve_stage_timeout_s` (``min_value=1``) and
    :func:`resolve_stage_max_retries` (``min_value=0``): both read a
    ``DEILE_PIPELINE_<AXIS>_<STAGE>`` env var as the highest-precedence override
    and fail-fast on a present-but-invalid value — a non-integer or one below
    the floor — so an operator config error surfaces loud instead of silently
    falling through to the next precedence level. An empty/whitespace value is
    treated as unset (``None``), letting the caller fall through.
    """
    raw_env = os.environ.get(env_name)
    if not raw_env or not raw_env.strip():
        return None
    try:
        value = int(raw_env.strip())
    except ValueError as exc:
        raise ValueError(f"invalid {env_name}={raw_env!r}: {exc}") from exc
    if value < min_value:
        raise ValueError(f"{env_name} must be >= {min_value}, got {value!r}")
    return value


def resolve_stage_timeout_s(stage: str) -> int:
    """Returns per-stage dispatch timeout in seconds, falling back to global default.

    Fallback chain (high → low priority):

    1. ``DEILE_PIPELINE_TIMEOUT_S_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.timeouts_s.<stage>`` in settings.json — warn + skip on invalid.
    3. Global settings: ``pipeline_claude_timeout`` (claude-worker) or
       ``pipeline_deile_timeout`` (deile-worker), when set.
    4. Built-in: :data:`BUILT_IN_TIMEOUT_S_CLAUDE` / :data:`BUILT_IN_TIMEOUT_S_DEILE`.

    Raises:
        ValueError: stage not in :data:`PIPELINE_STAGES` (programming bug).
        ValueError: env var contains a non-positive integer (fail-fast).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    # 1. Per-stage env var (fail-fast; timeout floor is 1s).
    env_val = _parse_stage_int_env(
        f"DEILE_PIPELINE_TIMEOUT_S_{stage.upper()}", min_value=1
    )
    if env_val is not None:
        return env_val

    # 2. Per-stage settings (graceful)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_timeout_s_{stage}", None)
    if settings_val is not None and settings_val > 0:
        return settings_val

    # 3. Global settings fallback — dispatcher-aware (claude vs deile)
    dispatcher = resolve_stage_dispatcher(stage)
    if dispatcher == "claude-worker":
        global_val = settings.pipeline_claude_timeout
        if global_val is not None and global_val > 0:
            return global_val
        return BUILT_IN_TIMEOUT_S_CLAUDE
    else:
        global_val = settings.pipeline_deile_timeout
        if global_val is not None and global_val > 0:
            return global_val
        return BUILT_IN_TIMEOUT_S_DEILE


def resolve_stage_max_retries(stage: str) -> int:
    """Returns per-stage max retries, falling back to global default.

    Fallback chain (high → low priority):

    1. ``DEILE_PIPELINE_RETRIES_<STAGE>`` env var — fail-fast on invalid.
    2. ``pipeline.retries.<stage>`` in settings.json — warn + skip on invalid.
    3. ``pipeline.default_max_retries`` in settings.json (global default).
    4. Built-in: :data:`BUILT_IN_MAX_RETRIES` (3).

    Raises:
        ValueError: stage not in :data:`PIPELINE_STAGES` (programming bug).
        ValueError: env var contains a negative integer (fail-fast).
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    # 1. Per-stage env var (fail-fast; 0 retries is valid, so the floor is 0).
    env_val = _parse_stage_int_env(
        f"DEILE_PIPELINE_RETRIES_{stage.upper()}", min_value=0
    )
    if env_val is not None:
        return env_val

    # 2. Per-stage settings (graceful)
    from deile.config.settings import get_settings  # lazy: avoids import cycle
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_retries_{stage}", None)
    if settings_val is not None:
        return settings_val

    # 3. Global settings default
    if settings.pipeline_default_max_retries is not None:
        return settings.pipeline_default_max_retries

    # 4. Built-in
    return BUILT_IN_MAX_RETRIES


def resolve_stage_cost_cap_usd(stage: str) -> Optional[Decimal]:
    """Return per-stage cost cap in USD, or None if no cap configured.

    Fallback chain (5 levels — mirrors resolve_stage_dispatcher):

    1. ``DEILE_PIPELINE_COST_CAP_USD_<STAGE>`` env var — decimal string, e.g.
       ``"5.00"``.  Invalid value → ValueError fail-fast.
    2. ``pipeline.cost_caps_usd.<stage>`` in settings.json — graceful warn +
       skip on invalid.
    3. ``DEILE_PIPELINE_COST_CAP_USD`` env var (global fallback for all stages).
       Invalid → ValueError fail-fast.
    4. ``pipeline.cost_cap_usd`` in settings.json (global).  Graceful warn.
    5. ``None`` — no cap (current behavior, unlimited).

    Args:
        stage: canonical stage name.

    Returns:
        Positive Decimal in USD, or None when no cap is configured.

    Raises:
        ValueError: stage is not in PIPELINE_STAGES (programming bug).
        ValueError: an env var contains a non-positive or non-parseable value.
    """
    if stage not in PIPELINE_STAGES:
        raise ValueError(
            f"unknown stage {stage!r}; expected one of {PIPELINE_STAGES}"
        )

    def _parse_cap(raw: Optional[str], context: str, *, strict: bool) -> Optional[Decimal]:
        if not raw or not raw.strip():
            return None
        stripped = raw.strip()
        try:
            d = Decimal(stripped)
        except InvalidOperation as exc:
            msg = f"invalid decimal {stripped!r} for cost cap ({context})"
            if strict:
                raise ValueError(msg) from exc
            logger.warning("dispatch_resolver: %s — ignoring", msg)
            return None
        if d <= 0:
            msg = f"cost cap must be positive, got {d} ({context})"
            if strict:
                raise ValueError(msg)
            logger.warning("dispatch_resolver: %s — ignoring", msg)
            return None
        return d

    # 1. Per-stage env var (fail-fast on invalid).
    stage_env = os.environ.get(f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}")
    cap = _parse_cap(stage_env, f"DEILE_PIPELINE_COST_CAP_USD_{stage.upper()}", strict=True)
    if cap is not None:
        return cap

    # 2. Settings per-stage (graceful).
    from deile.config.settings import \
        get_settings  # noqa: PLC0415 — lazy import
    settings = get_settings()
    settings_val = getattr(settings, f"pipeline_cost_cap_usd_{stage}", None)
    if settings_val is not None:
        if isinstance(settings_val, Decimal) and settings_val > 0:
            return settings_val
        # Non-Decimal or non-positive — log and fall through.
        logger.warning(
            "dispatch_resolver: invalid cost cap %r in settings.json "
            "(pipeline.cost_caps_usd.%s) — ignoring",
            settings_val, stage,
        )

    # 3. Global env var fallback (fail-fast on invalid).
    global_env = os.environ.get("DEILE_PIPELINE_COST_CAP_USD")
    cap = _parse_cap(global_env, "DEILE_PIPELINE_COST_CAP_USD", strict=True)
    if cap is not None:
        return cap

    # 4. Global settings (graceful).
    global_settings = getattr(settings, "pipeline_cost_cap_usd", None)
    if global_settings is not None:
        if isinstance(global_settings, Decimal) and global_settings > 0:
            return global_settings

    # 5. No cap.
    return None


def get_endpoint_for(dispatcher: str) -> str:
    """Resolve a URL HTTP do worker pod *dispatcher*.

    A env var de endpoint do worker sobrescreve o default — útil para dev local
    que aponta para localhost em vez do Service DNS do cluster. Workers núcleo
    usam ``DEILE_WORKER_ENDPOINT`` / ``DEILE_CLAUDE_WORKER_ENDPOINT``; cada worker
    da frota CLI usa ``DEILE_<KIND>_WORKER_ENDPOINT`` e tem o default
    ``http://<kind>-worker:<default_port>`` DERIVADO do registro de adapters
    (porta nunca hardcodada aqui).

    Raises:
        ValueError: dispatcher fora de :func:`get_valid_dispatchers`.
    """
    canonical = _canonicalize(dispatcher)
    if canonical is None:
        raise ValueError(f"unknown dispatcher {dispatcher!r}")
    default_url, env_var = _endpoint_for_dispatcher(canonical)
    if env_var:
        return os.environ.get(env_var) or default_url
    return default_url
