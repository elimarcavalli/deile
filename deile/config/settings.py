"""Sistema de configurações do DEILE.

Layered settings (issue #111):
  Project ``.deile/settings.json`` > User ``~/.deile/settings.json`` > defaults.

The legacy ``config/settings.json`` flow is still recognized as a one-shot
fallback (with a deprecation log). New writes go to the user's
``~/.deile/settings.json`` via :class:`SettingsManager`.
"""

import json
import logging
import os
import re
import threading
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_DEILE_DEPRECATED_ENV_VARS: Dict[str, str] = {
    "DEILE_PREFERRED_MODEL": "model.preferred",
    "DEILE_VISION_MODEL": "model.vision_model",
    "DEILE_BOT_APPROVAL_AUTO": "approval.auto",
    "DEILE_LOOP_GUARD_DISABLE": "loop_guard.disabled",
    "DEILE_LOOP_GUARD_MAX_CALLS": "loop_guard.max_calls",
    "DEILE_LOOP_GUARD_REPEAT_THRESHOLD": "loop_guard.repeat_threshold",
    "DEILE_LOOP_GUARD_WINDOW_SIZE": "loop_guard.window_size",
    "DEILE_LOOP_GUARD_WINDOW_THRESHOLD": "loop_guard.window_threshold",
    "DEILE_LOOP_GUARD_NO_PROGRESS": "loop_guard.no_progress",
    "DEILE_PIPELINE_BASE_PATH": "pipeline.base_path",
    "DEILE_PIPELINE_REPO": "pipeline.repo",
    "DEILE_PIPELINE_NOTIFY_USER_ID": "pipeline.notify_user_id",
    "DEILE_PIPELINE_POLL_INTERVAL": "pipeline.poll_interval",
    "DEILE_PIPELINE_CLAUDE_TIMEOUT": "pipeline.claude_timeout",
    "DEILE_PIPELINE_AUTOSTART": "pipeline.autostart",
    "DEILE_PIPELINE_DISPATCH_MODE": "pipeline.dispatch_mode",
    "DEILE_PIPELINE_RESUME_ENABLED": "pipeline.resume_enabled",
    "DEILE_PIPELINE_RESUME_INTERVAL": "pipeline.resume_interval",
    "DEILE_PIPELINE_RESUME_MAX_ATTEMPTS": "pipeline.resume_max_attempts",
    "DEILE_PIPELINE_RESUME_BUDGET": "pipeline.resume_budget",
    # Forge layer (issue #297) — every new env var routes through Settings
    # the same way the pipeline knobs do. ``DEILE_FORGE_REPO`` supersedes
    # ``DEILE_PIPELINE_REPO`` (above) but the legacy name keeps working.
    "DEILE_FORGE_REPO": "forge.repo",
    "DEILE_FORGE_KIND": "forge.kind",
    "DEILE_GITHUB_HOST": "forge.github_host",
    "DEILE_GITLAB_HOST": "forge.gitlab_host",
    "DEILE_FORGE_PROBE": "forge.probe_enabled",
    "DEILE_FORGE_BOT_LOGIN": "forge.bot_login",
    "DEILE_GITLAB_API_VERSION": "forge.gitlab_api_version",
    "DEILE_GITHUB_API_PREFIX": "forge.github_api_prefix",
    "DEILE_CRON_DB_PATH": "cron.db_path",
    "DEILE_CRON_POLL_INTERVAL": "cron.poll_interval",
    "DEILE_DEBUG": "debug.enabled",
}


class LogLevel(Enum):
    """Níveis de log disponíveis"""

    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


# ---------------------------------------------------------------------------
# Override mapping (issue #111)
# ---------------------------------------------------------------------------


def _to_log_level(value: Any) -> LogLevel:
    if isinstance(value, LogLevel):
        return value
    return LogLevel(str(value).upper())


def _to_bool(value: Any) -> bool:
    """Strict bool coercion — rejects ambiguous string literals."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        v = value.lower().strip()
        if v in ("true", "1", "yes", "on"):
            return True
        if v in ("false", "0", "no", "off"):
            return False
        raise ValueError(f"Cannot coerce {value!r} to bool")
    raise TypeError(f"Expected bool, got {type(value).__name__}")


def _to_str_list(value: Any) -> list:
    """Strict list coercion — rejects non-list values."""
    if isinstance(value, list):
        return [str(v) for v in value]
    raise TypeError(f"Expected list, got {type(value).__name__}")


def _to_optional_path(value: Any) -> Optional[Path]:
    """Convert value to Path, hardened against null bytes and oversized values."""
    if value is None or (isinstance(value, str) and not value):
        return None
    if not isinstance(value, (str, os.PathLike)):
        raise TypeError(f"path setting must be string, got {type(value).__name__}")
    s = str(value)
    if "\x00" in s:
        raise ValueError("path setting contains null byte")
    if len(s) > 4096:
        raise ValueError(f"path setting exceeds 4096 chars (got {len(s)})")
    return Path(s).expanduser()


def _mb_to_bytes(value: Any) -> int:
    return int(value) * 1024 * 1024


def _to_nonneg_int(value: Any) -> int:
    """Coerce to a non-negative int (rejects negatives and bools).

    Used by the pipeline resume knobs (interval/max_attempts/budget) where 0 is
    a meaningful value (``interval=0`` = immediate, ``budget=0`` = no ceiling)
    but a negative would be nonsensical.
    """
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    iv = int(value)
    if iv < 0:
        raise ValueError(f"value must be >= 0, got {iv}")
    return iv


def _to_pos_int(value: Any) -> int:
    """Coerce to a positive int (>= 1; rejects 0/negatives and bools).

    Used by ``resume_max_attempts``: a 0 or negative ceiling would make
    ``attempt >= max_attempts`` true on the first check and block every resume
    instantly. A rejected value is caught by ``apply_overrides`` and leaves the
    default (10) in place.
    """
    if isinstance(value, bool):
        raise TypeError("expected int, got bool")
    iv = int(value)
    if iv < 1:
        raise ValueError(f"value must be >= 1, got {iv}")
    return iv


# Per-stage pipeline model slug (issue #305): ``provider:model``. Mirrors
# `_MODEL_SLUG_RE` in `deile/infrastructure/deile_worker_client.py` — keep in
# sync (both validate the same wire/JSON format).
_MODEL_SLUG_RE = re.compile(r"^[a-z][a-z0-9_-]*:[a-z0-9._-]+$")


def _to_optional_model_slug(value: Any) -> Optional[str]:
    """Strict converter for ``pipeline.models.<stage>`` entries (issue #305).

    ``None`` and empty/whitespace string collapse to ``None`` (no override).
    Non-strings, or strings that don't match ``provider:model``, raise —
    ``apply_overrides`` catches the exception and keeps the previous (default)
    value. Strict by design: a typo here would silently route every dispatch
    to a non-existent model, manifesting only as a worker-side 5xx many
    minutes later.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    if not _MODEL_SLUG_RE.match(stripped):
        raise ValueError(
            f"invalid model slug {stripped!r}; expected 'provider:model'"
        )
    return stripped


def _to_optional_dispatcher(value: Any) -> Optional[str]:
    """Strict converter for ``pipeline.dispatchers.<stage>`` entries (issue #309).

    Espelha ``_to_optional_model_slug``: ``None`` / vazio colapsa para ``None``
    (sem override); non-string ou valor fora do whitelist do
    :func:`is_valid_dispatcher` levanta — ``apply_overrides`` engole e mantém
    o valor anterior. Strict by design: um typo aqui rotearia silenciosamente
    todo dispatch para o engine errado (claude-worker dispara billing de
    subscription/API anthropic, deile-worker usa o provider configurado).

    Note: o validator aceita aliases legacy de PR #330 (``deile_worker``,
    ``claude_code``, etc.); a canonicalização para ``deile-worker`` /
    ``claude-worker`` é responsabilidade do :mod:`dispatch_resolver` no
    momento da resolução, não da camada de persistência.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise TypeError(f"expected str, got {type(value).__name__}")
    stripped = value.strip()
    if not stripped:
        return None
    # Lazy import — evita import cycle settings → pipeline.dispatch_resolver →
    # settings (resolver consome ``get_settings()`` em runtime).
    from deile.orchestration.pipeline.dispatch_resolver import \
        is_valid_dispatcher
    if not is_valid_dispatcher(stripped):
        raise ValueError(
            f"invalid dispatcher {stripped!r}; expected one of "
            "'deile-worker'/'claude-worker' (or legacy aliases "
            "'deile_worker', 'claude_code', 'worker', 'claude', etc)"
        )
    return stripped


# Map of nested JSON paths in ``.deile/settings.json`` to ``Settings`` flat
# fields, with a converter for each. Unknown keys are silently ignored —
# that's how future-compatible forward-compat works.
#
# This is the **single source of truth** for known dotted-keys with strict
# type converters. ``apply_overrides`` and ``Settings.load_from_file`` use
# it directly (strict path, issue #125 hardening). The looser path used by
# ``_apply_nested_dict`` (see ``_JSON_FIELD_MAP`` below) derives the field
# name from this map plus a small addendum of JSON-only keys
# (``_JSON_ONLY_FIELD_MAP``) — adding a key here automatically makes it
# accepted by the layered loader, without needing a parallel entry.
_OVERRIDE_HANDLERS: Dict[str, Tuple[str, Callable[[Any], Any]]] = {
    "logging.level": ("log_level", _to_log_level),
    "logging.to_file": ("log_to_file", _to_bool),
    "logging.max_size_mb": ("log_file_max_size", _mb_to_bytes),
    "logging.backup_count": ("log_file_backup_count", int),
    "ui.streaming_enabled": ("streaming_enabled", _to_bool),
    "ui.show_tool_details": ("show_tool_details", _to_bool),
    "model.default_provider": ("default_model_provider", str),
    "model.max_context_tokens": ("max_context_tokens", int),
    "caching.enabled": ("enable_caching", _to_bool),
    "caching.ttl_seconds": ("cache_ttl", int),
    "caching.parser_cache_enabled": ("parser_cache_enabled", _to_bool),
    "caching.parser_cache_ttl": ("parser_cache_ttl", int),
    "concurrency.max_concurrent_requests": ("max_concurrent_requests", int),
    "concurrency.request_timeout": ("request_timeout", int),
    "concurrency.max_tool_execution_time": ("max_tool_execution_time", int),
    "file_safety.enabled": ("enable_file_safety_checks", _to_bool),
    "file_safety.allowed_extensions": ("allowed_file_extensions", _to_str_list),
    "file_safety.blocked_directories": ("blocked_directories", _to_str_list),
    "file_safety.max_file_size_bytes": ("max_file_size_bytes", int),
    "file_safety.allow_all_types": ("allow_all_file_types", _to_bool),
    "file_safety.encoding_detection": ("file_encoding_detection", _to_bool),
    "deile_md.enabled": ("deile_md_enabled", _to_bool),
    "deile_md.user_path": ("deile_md_user_path", _to_optional_path),
    "deile_md.cwd_filename": ("deile_md_cwd_filename", str),
    "deile_md.max_bytes": ("deile_md_max_bytes", int),
    "environment": ("environment", str),
    "debug": ("debug", _to_bool),
    # Pipeline resume knobs (issue #254)
    "pipeline.resume_enabled": ("pipeline_resume_enabled", _to_bool),
    "pipeline.resume_interval": ("pipeline_resume_interval", _to_nonneg_int),
    "pipeline.resume_max_attempts": ("pipeline_resume_max_attempts", _to_pos_int),
    "pipeline.resume_budget": ("pipeline_resume_budget", _to_nonneg_int),
    # Refinement gate + parallel decomposition (issue #257)
    "pipeline.refine_max_attempts": ("pipeline_refine_max_attempts", _to_pos_int),
    "pipeline.max_parallel": ("pipeline_max_parallel", _to_pos_int),
    # Per-stage model override (issue #305) — see _MODEL_SLUG_RE / resolver.
    "pipeline.models.classify":   ("pipeline_model_classify",   _to_optional_model_slug),
    "pipeline.models.refine":     ("pipeline_model_refine",     _to_optional_model_slug),
    "pipeline.models.implement":  ("pipeline_model_implement",  _to_optional_model_slug),
    "pipeline.models.pr_review":  ("pipeline_model_pr_review",  _to_optional_model_slug),
    "pipeline.models.follow_ups": ("pipeline_model_follow_ups", _to_optional_model_slug),
    # Per-stage dispatcher override (issue #309) — see dispatch_resolver.
    "pipeline.dispatchers.classify":   ("pipeline_dispatcher_classify",   _to_optional_dispatcher),
    "pipeline.dispatchers.refine":     ("pipeline_dispatcher_refine",     _to_optional_dispatcher),
    "pipeline.dispatchers.implement":  ("pipeline_dispatcher_implement",  _to_optional_dispatcher),
    "pipeline.dispatchers.pr_review":  ("pipeline_dispatcher_pr_review",  _to_optional_dispatcher),
    "pipeline.dispatchers.follow_ups": ("pipeline_dispatcher_follow_ups", _to_optional_dispatcher),
    # Sub-DEILEs paralelos (issue #257)
    "subagent.runner": ("subagent_runner", lambda v: str(v).strip().lower()),
    "subagent.max_parallel": ("subagent_max_parallel", _to_pos_int),
    "subagent.poll_interval_s": ("subagent_poll_interval_s", float),
    "subagent.budget_s": ("subagent_budget_s", float),
    "subagent.capture_buffer_max_bytes": (
        "subagent_capture_buffer_max_bytes", _to_pos_int,
    ),
    # Trust boundary (issue #125): allowlist of directories whose
    # ``./.deile/settings.json`` is honored as the project layer.
    "trust.project_layer_dirs": ("trust_project_layer_dirs", _to_str_list),
    # Migration knob: 'auto' = honor non-allowlisted with loud warning;
    # 'deny' = ignore non-allowlisted silently. Default keeps legacy behavior
    # for one minor version, then flips to 'deny' in the next major.
    "trust.project_layer_default": ("trust_project_layer_default", str),
    # Enterprise/security profile settings (issue #138)
    "security.sandbox_code_execution": ("sandbox_code_execution", _to_bool),
    "security.encrypt_logs": ("encrypt_logs", _to_bool),
    "monitoring.generate_compliance_reports": ("generate_compliance_reports", _to_bool),
}


def _resolve_dotted(data: dict, key_path: str) -> Tuple[bool, Any]:
    """Walk *data* by dotted *key_path*. Returns (found, value)."""
    node: Any = data
    for part in key_path.split("."):
        if not isinstance(node, dict) or part not in node:
            return False, None
        node = node[part]
    return True, node


@dataclass
class Settings:
    """Configurações globais do DEILE"""

    # Básicas
    app_name: str = "DEILE"
    version: str = "5.1.0"
    debug: bool = False

    # Diretórios (cwd-bound, não configuráveis via JSON)
    working_directory: Path = field(default_factory=Path.cwd)
    config_directory: Path = field(default_factory=lambda: Path.cwd() / "config")
    logs_directory: Path = field(default_factory=lambda: Path.cwd() / "logs")
    cache_directory: Path = field(default_factory=lambda: Path.cwd() / "cache")

    # Logging
    log_level: LogLevel = LogLevel.DEBUG
    log_to_file: bool = True
    log_file_max_size: int = 10 * 1024 * 1024
    log_file_backup_count: int = 5

    # Modelo (DELEGADO ao ConfigManager; mantido como fallback)
    default_model_provider: str = "gemini"
    default_model_name: str = "gemini-1.5-pro-latest"
    max_context_tokens: int = 8000
    preferred_model: Optional[str] = None
    vision_model: str = "gemini-2.5-flash-lite"

    # Tools
    auto_discover_tools: bool = True
    enabled_tool_categories: List[str] = field(
        default_factory=lambda: ["file", "execution", "search"]
    )
    max_tool_execution_time: int = 30

    # Parsers
    auto_discover_parsers: bool = True
    parser_cache_enabled: bool = True
    parser_cache_ttl: int = 300

    # Contexto (DELEGADO ao ConfigManager)
    context_optimization_enabled: bool = True
    rag_enabled: bool = False
    semantic_search_enabled: bool = False

    # Performance
    max_concurrent_requests: int = 10
    request_timeout: int = 120
    enable_caching: bool = True
    cache_ttl: int = 3600

    # Streaming UI
    streaming_enabled: bool = True
    show_tool_details: bool = False

    # Segurança de arquivos
    enable_file_safety_checks: bool = True
    allowed_file_extensions: List[str] = field(
        default_factory=lambda: [
            ".py",
            ".js",
            ".ts",
            ".html",
            ".css",
            ".md",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
        ]
    )
    blocked_directories: List[str] = field(
        default_factory=lambda: [".git", "__pycache__", "node_modules", ".env"]
    )
    max_file_size_bytes: int = 1024 * 1024
    allow_all_file_types: bool = True
    file_encoding_detection: bool = True

    # Ambiente
    environment: str = "development"
    api_keys: Dict[str, str] = field(default_factory=dict)

    # DEILE.md hierarchical loader
    deile_md_enabled: bool = True
    deile_md_user_path: Optional[Path] = None
    deile_md_cwd_filename: str = "DEILE.md"
    deile_md_max_bytes: int = 64 * 1024

    # Loop guard
    loop_guard_disabled: bool = False
    loop_guard_max_calls: int = 50
    loop_guard_repeat_threshold: int = 3
    loop_guard_window_size: int = 5
    loop_guard_window_threshold: int = 3
    loop_guard_no_progress: int = 6

    # Aprovação de bots
    bot_approval_auto: bool = False

    # Debug
    debug_enabled: bool = False

    # Pipeline
    pipeline_base_path: Optional[Path] = None
    pipeline_repo: str = "elimarcavalli/deile"
    pipeline_notify_user_id: Optional[str] = None
    pipeline_poll_interval: int = 60
    pipeline_claude_timeout: int = 1800
    pipeline_autostart: bool = False  # gap #3: set True via DEILE_PIPELINE_AUTOSTART
    # Implementation/review strategy: "deile_worker" (DEILE-to-DEILE via the
    # worker Pod, the product default — Claude is optional) or "claude"
    # (claude -p one-shot in a local worktree). Set via DEILE_PIPELINE_DISPATCH_MODE.
    pipeline_dispatch_mode: str = "deile_worker"
    # Pipeline resume (issue #254): when an implement/review stops mid-way
    # (iteration cap, timeout/crash, or the agent declared INCOMPLETO), the
    # next attempt RESUMES the partial work (reusing the branch + untracked
    # files in the persistent workspace) instead of resetting to main.
    #   resume_enabled       — master switch; when False, parked work is never
    #                           auto-resumed (legacy "park forever" behaviour).
    #   resume_interval      — min seconds between resume attempts for the same
    #                           issue; 0 (default) = retry on the next free tick.
    #   resume_max_attempts  — hard ceiling on attempts per issue before the
    #                           block flow fires (comment + ~workflow:bloqueada + DM).
    #   resume_budget        — accumulated wall-clock seconds across attempts
    #                           before the block flow fires; 0 = no time ceiling.
    pipeline_resume_enabled: bool = True
    pipeline_resume_interval: int = 0
    pipeline_resume_max_attempts: int = 10
    pipeline_resume_budget: int = 0
    pipeline_refine_max_attempts: int = 5
    pipeline_max_parallel: int = 2

    # Forge layer (issue #297) — selects which provider (GitHub or GitLab)
    # backs the pipeline and the agent CLI. ``forge_repo`` is the new
    # canonical name for the project path; ``pipeline_repo`` above stays as
    # the deprecated alias (resolve_forge_repo prefers ``forge_repo`` when
    # both are set). ``forge_kind`` controls auto-detection: ``auto`` (the
    # default) uses the URL/path heuristics in :mod:`deile.orchestration.forge.detection`;
    # ``github`` / ``gitlab`` force the choice. ``forge_github_host`` and
    # ``forge_gitlab_host`` declare custom hosts for GHES / self-hosted
    # GitLab — the URL parser only accepts these explicitly (no guessing).
    # ``forge_probe_enabled`` enables an opt-in HTTP probe for hosts that
    # match neither cloud default (disabled by default — adds network).
    forge_repo: str = ""
    forge_kind: str = "auto"
    forge_github_host: str = "github.com"
    forge_gitlab_host: str = "gitlab.com"
    forge_probe_enabled: bool = False
    # ``forge_bot_login`` is the mention handle the pipeline watches for —
    # the canonical handle is ``@deile-one`` on both forges. Centralised
    # here so a fork can override without touching code.
    forge_bot_login: str = "@deile-one"
    forge_gitlab_api_version: str = "4"
    forge_github_api_prefix: str = "api"

    # Pipeline per-stage model override (issue #305) — local-CLI path. The
    # cluster path uses `DEILE_PIPELINE_MODEL_<STAGE>` env on the worker
    # Deployment (kubectl set env via the panel TUI). Both layers are read
    # by `resolve_stage_model` in `deile/orchestration/pipeline/model_resolver`.
    # Worker forwards the resolved slug as `DispatchPayload.preferred_model`,
    # which the worker injects in `session.context_data["preferred_model"]`
    # — the agent picks it up via the soft-override chain.
    pipeline_model_classify: Optional[str] = None
    pipeline_model_refine: Optional[str] = None
    pipeline_model_implement: Optional[str] = None
    pipeline_model_pr_review: Optional[str] = None
    pipeline_model_follow_ups: Optional[str] = None

    # Pipeline per-stage dispatcher override (issue #309) — CLI persistence layer.
    # Decide qual worker pod recebe o ``POST /v1/dispatch`` por etapa:
    # ``deile-worker`` (DEILE-to-DEILE, default; usa o provider configurado)
    # ou ``claude-worker`` (subprocesso ``claude -p``; força modelos anthropic).
    # Aceita aliases legacy de PR #330 (``deile_worker``, ``claude_code``,
    # ``worker``, ``claude``); canonicalização fica a cargo do
    # ``dispatch_resolver`` no momento da resolução. Cluster path equivalente
    # usa ``DEILE_PIPELINE_DISPATCH_<STAGE>`` env nos pods da Deployment.
    # Stages sem override caem no global ``DEILE_PIPELINE_DISPATCH_MODE``;
    # sem isso, ``deile-worker`` built-in.
    pipeline_dispatcher_classify: Optional[str] = None
    pipeline_dispatcher_refine: Optional[str] = None
    pipeline_dispatcher_implement: Optional[str] = None
    pipeline_dispatcher_pr_review: Optional[str] = None
    pipeline_dispatcher_follow_ups: Optional[str] = None

    # Sub-DEILEs paralelos em sessão CLI (issue #257)
    # `subagent_runner`        — "local" (default; in-process via asyncio.gather de
    #                            DeileAgent.process_input_stream em sessões limpas)
    #                            ou "worker" (delega ao deile-worker HTTP).
    # `subagent_max_parallel`  — teto de concorrência por chamada da tool.
    # `subagent_poll_interval_s` — período de polling do WorkerSubAgentRunner.
    subagent_runner: str = "local"
    subagent_max_parallel: int = 3
    subagent_poll_interval_s: float = 0.8
    # Teto global de tempo da invocação do tool `dispatch_parallel_subagents`
    # (M2/M11 — issue #295 review). Default = 10min = mesmo budget do worker.
    subagent_budget_s: float = 600.0
    # Cap (em bytes) do buffer de captura de stdout/stderr por sub-DEILE
    # (item 10). Default histórico = 256 KiB. Overridable via env
    # ``DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES`` ou JSON
    # ``subagent.capture_buffer_max_bytes``.
    subagent_capture_buffer_max_bytes: int = 256 * 1024

    # Cron
    cron_db_path: Optional[Path] = None
    cron_poll_interval: int = 30

    # Agent tool-loop
    # Max function-calling rounds per turn before the loop is force-stopped. A
    # real implementation (read files + edit + test + commit + push + open PR)
    # easily exceeds a low cap, so the agent would stop before finishing.
    # Override via DEILE_MAX_TOOL_ITERATIONS or settings.json
    # `agent.max_tool_iterations`.
    max_tool_iterations: int = 100

    # Perfil e skills (lidos via SettingsManager, mantidos aqui para conveniência)
    profile_name: str = "autonomous_agent"
    skills_paths: List[str] = field(default_factory=list)

    # Trust boundary (issue #125)
    # `trust.project_layer_dirs` — allowlist of absolute directories whose
    #   ``./.deile/settings.json`` is honored as the project layer at boot.
    # `trust.project_layer_default` — migration knob:
    #   - "auto" (default): honor non-allowlisted with a loud warning.
    #     Will flip to "deny" in the next major.
    #   - "deny": ignore non-allowlisted silently (after one warning).
    trust_project_layer_dirs: List[str] = field(default_factory=list)
    trust_project_layer_default: str = "auto"

    # Enterprise/security profile settings (issue #138)
    # When True these flags gate behavior in bash_tool, logs, and audit_logger.
    sandbox_code_execution: bool = False
    encrypt_logs: bool = False
    generate_compliance_reports: bool = False

    def __post_init__(self) -> None:
        for attr in ("working_directory", "config_directory", "logs_directory", "cache_directory"):
            v = getattr(self, attr)
            if isinstance(v, str):
                setattr(self, attr, Path(v))
        for attr in ("pipeline_base_path", "cron_db_path", "deile_md_user_path"):
            v = getattr(self, attr)
            if isinstance(v, str):
                setattr(self, attr, Path(v))

        if not self.api_keys:
            self.api_keys = self._load_api_keys_from_env()

    def _load_api_keys_from_env(self) -> Dict[str, str]:
        keys: Dict[str, str] = {}
        for key in (
            "GOOGLE_API_KEY",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "AZURE_API_KEY",
            "DEEPSEEK_API_KEY",
        ):
            value = os.getenv(key)
            if value:
                keys[key] = value
        return keys

    def _create_directories(self) -> None:
        for directory in (self.config_directory, self.logs_directory, self.cache_directory):
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                logger.warning("Could not create directory %s: %s", directory, exc)

    def to_dict(self, exclude_api_keys: bool = True) -> Dict[str, Any]:
        result: Dict[str, Any] = {}
        for key, value in self.__dict__.items():
            if exclude_api_keys and key == "api_keys":
                continue
            if isinstance(value, Path):
                result[key] = str(value)
            elif isinstance(value, LogLevel):
                result[key] = value.value
            else:
                result[key] = value
        return result

    def update(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
            else:
                logger.warning("Unknown setting: %s", key)

    def apply_overrides(self, data: dict) -> None:
        """Apply nested ``.deile/settings.json`` overrides to flat fields.

        Walks ``_OVERRIDE_HANDLERS`` and pulls each known key out of *data*.
        Unknown keys are silently ignored (forward-compatible). Conversion
        errors leave the existing default in place plus a warning log.

        This method uses a two-pass approach: first validates all keys, then
        applies them — preventing partial application from crashing mid-way.
        """
        if not isinstance(data, dict) or not data:
            return
        pending: list = []
        for key_path, (field_name, converter) in _OVERRIDE_HANDLERS.items():
            found, raw = _resolve_dotted(data, key_path)
            if not found:
                continue
            try:
                pending.append((field_name, converter(raw)))
            except (ValueError, TypeError) as exc:
                logger.warning(
                    "settings: cannot apply %s=%r (%s); skipping this key",
                    key_path, raw, exc,
                )
        for field_name, value in pending:
            if not hasattr(self, field_name):
                logger.error("settings: handler targets non-existent field %r", field_name)
                continue
            setattr(self, field_name, value)

    def save_to_file(self, file_path: Optional[str] = None) -> bool:
        """DEPRECATED. Use SettingsManager.set_setting() instead."""
        if file_path is None:
            logger.warning(
                "Settings.save_to_file() called without path is a no-op. "
                "Use SettingsManager.set_setting() to persist preferences."
            )
            return False
        try:
            config_dict = self.to_dict()
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(config_dict, f, indent=2, default=str)
            logger.info("Settings saved to %s", file_path)
            return True
        except OSError as e:
            logger.error("Failed to save settings to %s: %s", file_path, e)
            return False

    @classmethod
    def load_from_file(cls, file_path: Path) -> "Settings":
        """DEPRECATED (issue #111): read via SettingsManager + apply_overrides.

        Kept so existing callers passing an explicit path don't break;
        ``get_settings()`` no longer routes through this method.

        Hardened (issue #125): the legacy flat-key flow now filters
        ``config_dict`` against an explicit allowlist of safe attribute
        names — the same set ``_OVERRIDE_HANDLERS`` accepts in the layered
        flow. Unknown keys are logged as warning and discarded so a hostile
        ``config/settings.json`` cannot flip arbitrary internal flags
        (e.g. ``working_directory='/etc'`` or ``allow_all_file_types=True``).
        """
        try:
            with open(file_path, encoding="utf-8") as f:
                config_dict = json.load(f)

            if not isinstance(config_dict, dict):
                logger.warning(
                    "settings: legacy file %s is not a JSON object; using defaults",
                    file_path,
                )
                return cls()

            if "api_keys" in config_dict:
                logger.warning("API keys found in config file. Ignoring them for security.")
                del config_dict["api_keys"]

            # Allowlist filter (issue #125): only attribute names that map
            # back to a known _OVERRIDE_HANDLERS entry are accepted. The
            # handler set is the canonical surface for "safe to load from
            # untrusted JSON".
            #
            # P1-4: not just NAMES — VALUES must also pass through the same
            # type-converters used by ``apply_overrides``. Otherwise
            # ``enable_file_safety_checks: "yes-please"`` (string) would
            # land verbatim on a bool-typed field, and
            # ``trust_project_layer_dirs: "/single"`` (string) would
            # bypass the list constraint enforced by ``_to_str_list``.
            reverse_handlers: Dict[str, Callable[[Any], Any]] = {
                field_name: converter
                for field_name, converter in _OVERRIDE_HANDLERS.values()
            }
            filtered: Dict[str, Any] = {}
            dropped: List[str] = []
            invalid: List[str] = []
            for key, value in config_dict.items():
                if key not in reverse_handlers:
                    dropped.append(key)
                    continue
                converter = reverse_handlers[key]
                try:
                    filtered[key] = converter(value)
                except (TypeError, ValueError) as exc:
                    invalid.append(f"{key}({exc})")
            if dropped:
                logger.warning(
                    "settings: legacy file %s contains keys outside the "
                    "allowlist; ignoring %s",
                    file_path,
                    ", ".join(sorted(dropped)),
                )
            if invalid:
                logger.warning(
                    "settings: legacy file %s contains values that failed "
                    "type-conversion; ignoring %s",
                    file_path,
                    ", ".join(sorted(invalid)),
                )

            settings = cls(**filtered)
            logger.info("Settings loaded from %s", file_path)
            return settings

        except (OSError, ValueError, TypeError) as e:
            logger.warning("Failed to load settings from %s: %s", file_path, e)
            logger.info("Using default settings")
            return cls()

    def get_api_key(self, provider: str) -> Optional[str]:
        key_mapping = {
            "gemini": "GOOGLE_API_KEY",
            "google": "GOOGLE_API_KEY",
            "openai": "OPENAI_API_KEY",
            "gpt": "OPENAI_API_KEY",
            "anthropic": "ANTHROPIC_API_KEY",
            "claude": "ANTHROPIC_API_KEY",
            "azure": "AZURE_API_KEY",
            "deepseek": "DEEPSEEK_API_KEY",
        }
        key_name = key_mapping.get(provider.lower())
        return self.api_keys.get(key_name) if key_name else None

    def is_development(self) -> bool:
        return self.environment == "development"

    def is_production(self) -> bool:
        return self.environment == "production"

    def get_config_manager(self) -> Any:
        try:
            from .manager import get_config_manager

            return get_config_manager()
        except ImportError:
            logger.warning("ConfigManager not available, using fallback settings")
            return None

    def get_model_config(self) -> Dict[str, Any]:
        config_manager = self.get_config_manager()
        if config_manager:
            config = config_manager.get_config()
            return {
                "model_name": config.gemini.model_name,
                "temperature": config.gemini.generation_config.get("temperature", 0.1),
                "max_context_tokens": config.agent.max_context_tokens,
                "generation_config": config.gemini.generation_config,
                "tool_config": config.gemini.tool_config,
                "safety_settings": config.gemini.safety_settings,
            }
        return {"model_name": "gemini-1.5-pro-latest", "temperature": 0.1, "max_context_tokens": 8000}

    def validate(self) -> List[str]:
        issues: List[str] = []
        if not self.get_api_key(self.default_model_provider):
            issues.append(f"Missing API key for default provider: {self.default_model_provider}")
        if not self.working_directory.exists():
            issues.append(f"Working directory does not exist: {self.working_directory}")
        config_manager = self.get_config_manager()
        if config_manager:
            try:
                config = config_manager.get_config()
                issues.extend(config.validate())
            except Exception as exc:
                issues.append(f"ConfigManager validation failed: {exc}")
        if self.max_concurrent_requests <= 0:
            issues.append("max_concurrent_requests must be positive")
        return issues

    def __str__(self) -> str:
        return f"Settings(env={self.environment}, provider={self.default_model_provider})"

    def __repr__(self) -> str:
        return f"<Settings: {self.app_name} v{self.version}>"


# ---------------------------------------------------------------------------
# JSON loading helpers (no cross-package deps — keeps config/ self-contained)
# ---------------------------------------------------------------------------


def _load_json_file(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Cannot read settings file %s: %s", path, exc)
        return {}


# Layered-loader addendum: dotted JSON keys that ARE accepted by the looser
# ``_apply_nested_dict`` path but are NOT in the strict ``_OVERRIDE_HANDLERS``
# above. These bypass strict converters and reach ``_set_typed`` directly,
# which refuses obvious type mismatches (issue #125 P1-1) but does not run
# the strict ValueError-on-unknown converters from ``_OVERRIDE_HANDLERS``.
#
# Keys here ARE NOT accepted by ``Settings.apply_overrides`` /
# ``Settings.load_from_file`` (strict path) by design — they're typically
# pipeline/cron/loop-guard knobs that travel via env vars or ad-hoc nested
# layouts. Promote a key from here to ``_OVERRIDE_HANDLERS`` only when you
# want it to gain a strict converter.
_JSON_ONLY_FIELD_MAP: Dict[str, str] = {
    "debug.enabled": "debug_enabled",
    "model.preferred": "preferred_model",
    "model.vision_model": "vision_model",
    "loop_guard.disabled": "loop_guard_disabled",
    "loop_guard.max_calls": "loop_guard_max_calls",
    "loop_guard.repeat_threshold": "loop_guard_repeat_threshold",
    "loop_guard.window_size": "loop_guard_window_size",
    "loop_guard.window_threshold": "loop_guard_window_threshold",
    "loop_guard.no_progress": "loop_guard_no_progress",
    "approval.auto": "bot_approval_auto",
    "skills_paths": "skills_paths",
    "profile.name": "profile_name",
    "pipeline.base_path": "pipeline_base_path",
    "pipeline.repo": "pipeline_repo",
    "pipeline.poll_interval": "pipeline_poll_interval",
    "pipeline.claude_timeout": "pipeline_claude_timeout",
    "pipeline.notify_user_id": "pipeline_notify_user_id",
    "pipeline.autostart": "pipeline_autostart",
    "pipeline.dispatch_mode": "pipeline_dispatch_mode",
    # Forge layer (issue #297). These JSON paths are accepted by the
    # loose loader; the env vars in ``_ENV_OVERRIDE_MAP`` resolve to these
    # same paths.
    "forge.repo": "forge_repo",
    "forge.kind": "forge_kind",
    "forge.github_host": "forge_github_host",
    "forge.gitlab_host": "forge_gitlab_host",
    "forge.probe_enabled": "forge_probe_enabled",
    "forge.bot_login": "forge_bot_login",
    "forge.gitlab_api_version": "forge_gitlab_api_version",
    "forge.github_api_prefix": "forge_github_api_prefix",
    "cron.db_path": "cron_db_path",
    "cron.poll_interval": "cron_poll_interval",
    # Sub-DEILEs paralelos (issue #257)
    "subagent.runner": "subagent_runner",
    "subagent.max_parallel": "subagent_max_parallel",
    "subagent.poll_interval_s": "subagent_poll_interval_s",
    "subagent.budget_s": "subagent_budget_s",
    "agent.max_tool_iterations": "max_tool_iterations",
}


def _build_json_field_map() -> Dict[str, str]:
    """Derive the loose-path field map from ``_OVERRIDE_HANDLERS`` + addendum.

    Every key in ``_OVERRIDE_HANDLERS`` whose Settings field is also reachable
    via the layered loader is reused here for its field name (the converter is
    dropped — the strict path runs converters, the loose path defers to
    ``_set_typed``'s type coercion). The 4 strict-only keys (``debug``,
    ``deile_md.user_path``, ``pipeline.max_parallel``,
    ``pipeline.refine_max_attempts``) are intentionally NOT promoted to the
    loose path — preserving the historical asymmetry. Keys exclusive to the
    loose path live in ``_JSON_ONLY_FIELD_MAP``.

    The two strict-only keys that historically had no loose-path twin
    (``debug`` and ``deile_md.user_path``) are excluded explicitly so this
    refactor is observably no-op vs. the previous hand-maintained mapping.
    """
    # Keys present strict-only by design — preserve historical asymmetry.
    _STRICT_ONLY = {
        "debug",
        "deile_md.user_path",
        "pipeline.max_parallel",
        "pipeline.refine_max_attempts",
    }
    derived = {
        key_path: field_name
        for key_path, (field_name, _conv) in _OVERRIDE_HANDLERS.items()
        if key_path not in _STRICT_ONLY
    }
    # Loose-only additions override never collide (the addendum has disjoint
    # keys from _OVERRIDE_HANDLERS by construction), but use a single update
    # for resilience to future edits.
    derived.update(_JSON_ONLY_FIELD_MAP)
    return derived


# Cached at module load so ``_apply_nested_dict`` can do an O(1) lookup
# without recomputing on every key. Treat as immutable from outside the
# module.
_JSON_FIELD_MAP: Dict[str, str] = _build_json_field_map()


# P1-1: list-typed Settings attributes. ``_set_typed`` refuses to coerce a
# non-list into one of these — silently storing a string here turns
# ``_is_project_layer_trusted`` into a per-character iterator (the original
# reviewer finding for ``trust_project_layer_dirs``).
_LIST_ATTRS: frozenset = frozenset({
    "trust_project_layer_dirs",
    "allowed_file_extensions",
    "blocked_directories",
    "skills_paths",
    "enabled_tool_categories",
})


def _apply_nested_dict(settings: "Settings", data: Dict[str, Any], prefix: str = "") -> None:
    """Recursively walk a nested dict and apply matching fields to settings."""
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if full_key in _JSON_FIELD_MAP:
            attr = _JSON_FIELD_MAP[full_key]
            _set_typed(settings, attr, value)
        elif isinstance(value, dict):
            _apply_nested_dict(settings, value, full_key)


def _set_typed(settings: "Settings", attr: str, value: Any) -> None:
    """Set a Settings attribute, coercing types as needed.

    P1-1 (issue #125): list-typed attributes (see ``_LIST_ATTRS``) refuse
    non-list inputs outright. Previously, ``trust.project_layer_dirs:
    "/single/path"`` (a string instead of a list) was stored verbatim and
    later iterated character-by-character in
    ``_is_project_layer_trusted`` — turning a configuration error into
    nonsensical behavior at best and a trust-boundary bypass at worst.
    """
    # P1-1: list-typed attributes — never accept a non-list passthrough.
    if attr in _LIST_ATTRS:
        if not isinstance(value, list):
            logger.warning(
                "settings: rejecting non-list value for %s (got %s); keeping previous value",
                attr,
                type(value).__name__,
            )
            return
        # Coerce list items to str for known str-list fields.
        try:
            setattr(settings, attr, [str(v) for v in value])
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning("Cannot apply setting %s=<list>: %s", attr, exc)
        return

    current = getattr(settings, attr, None)
    try:
        if isinstance(current, bool):
            # P1-1: bool field — only accept actual bools or whitelisted
            # string literals. ``"yes-please"`` previously slipped through
            # as truthy (str.lower() not in whitelist → False).
            if isinstance(value, bool):
                pass  # already bool
            elif isinstance(value, str):
                norm = value.strip().lower()
                if norm in {"1", "true", "yes", "on"}:
                    value = True
                elif norm in {"0", "false", "no", "off"}:
                    value = False
                else:
                    logger.warning(
                        "settings: rejecting ambiguous bool string for %s=%r",
                        attr,
                        value,
                    )
                    return
            elif isinstance(value, (int, float)):
                value = bool(value)
            else:
                logger.warning(
                    "settings: rejecting non-bool value for %s (got %s)",
                    attr,
                    type(value).__name__,
                )
                return
        elif isinstance(current, int) and not isinstance(value, bool):
            value = int(value)
            # Symmetry with the DEILE_MAX_TOOL_ITERATIONS env path
            # (``max(1, int(raw))``): a non-positive cap would disable tool use,
            # so clamp the settings.json path too instead of relying on a
            # downstream consumer to neutralise it.
            if attr == "max_tool_iterations":
                value = max(1, value)
        elif isinstance(current, Path) or (current is None and attr in (
            "pipeline_base_path", "cron_db_path", "deile_md_user_path"
        )):
            value = Path(value) if value is not None else None
        elif isinstance(current, LogLevel):
            value = LogLevel(str(value).upper())
        setattr(settings, attr, value)
    except Exception as exc:
        logger.warning("Cannot apply setting %s=%r: %s", attr, value, exc)


def _env_bool(raw: str) -> bool:
    """Lenient bool coercion for env vars: unknown → False.

    Matches the dominant ``raw.strip().lower() in {"1","true","yes","on"}``
    idiom used historically across DEILE_* boolean envs, and is more lenient
    than the strict ``_to_bool`` (which raises on unknown strings).
    """
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _int_floor(floor: int) -> Callable[[str], int]:
    """Build a converter that parses int and clamps below to ``floor``."""
    def _convert(raw: str) -> int:
        return max(floor, int(raw))
    return _convert


def _resolved_path(raw: str) -> Path:
    """Convert env-var raw string to absolute resolved Path."""
    return Path(raw).resolve()


# Table-driven env-var → settings attribute mapping. Each row converts the env
# var's raw string with ``convert``; ValueError is swallowed (legacy behavior
# preserved). ``deprecated=True`` rows emit a deprecation pointer to the
# matching key in ``_DEILE_DEPRECATED_ENV_VARS`` (issue #111 migration path).
_ENV_OVERRIDES: Tuple[Tuple[str, str, Callable[[str], Any], bool], ...] = (
    # (env_var, settings_attr, converter, deprecated)
    ("DEILE_DEBUG",                          "debug_enabled",                  _env_bool,         True),
    ("DEILE_PREFERRED_MODEL",                "preferred_model",                str,               True),
    ("DEILE_VISION_MODEL",                   "vision_model",                   str.strip,         True),
    ("DEILE_BOT_APPROVAL_AUTO",              "bot_approval_auto",              _env_bool,         True),
    ("DEILE_LOOP_GUARD_DISABLE",             "loop_guard_disabled",            _env_bool,         True),
    ("DEILE_LOOP_GUARD_MAX_CALLS",           "loop_guard_max_calls",           _int_floor(1),     True),
    ("DEILE_LOOP_GUARD_REPEAT_THRESHOLD",    "loop_guard_repeat_threshold",    _int_floor(1),     True),
    ("DEILE_LOOP_GUARD_WINDOW_SIZE",         "loop_guard_window_size",         _int_floor(1),     True),
    ("DEILE_LOOP_GUARD_WINDOW_THRESHOLD",    "loop_guard_window_threshold",    _int_floor(1),     True),
    ("DEILE_LOOP_GUARD_NO_PROGRESS",         "loop_guard_no_progress",         _int_floor(1),     True),
    ("DEILE_PIPELINE_BASE_PATH",             "pipeline_base_path",             _resolved_path,    True),
    ("DEILE_PIPELINE_REPO",                  "pipeline_repo",                  str,               True),
    ("DEILE_PIPELINE_NOTIFY_USER_ID",        "pipeline_notify_user_id",        str,               True),
    ("DEILE_PIPELINE_POLL_INTERVAL",         "pipeline_poll_interval",         int,               True),
    ("DEILE_PIPELINE_CLAUDE_TIMEOUT",        "pipeline_claude_timeout",        int,               True),
    # PIPELINE_AUTOSTART is a current knob (no deprecation warning).
    ("DEILE_PIPELINE_AUTOSTART",             "pipeline_autostart",             _env_bool,         False),
    ("DEILE_PIPELINE_DISPATCH_MODE",         "pipeline_dispatch_mode",         lambda s: s.strip().lower(), True),
    ("DEILE_PIPELINE_RESUME_ENABLED",        "pipeline_resume_enabled",        _env_bool,         True),
    ("DEILE_PIPELINE_RESUME_INTERVAL",       "pipeline_resume_interval",       _int_floor(0),     True),
    ("DEILE_PIPELINE_RESUME_MAX_ATTEMPTS",   "pipeline_resume_max_attempts",   _int_floor(1),     True),
    ("DEILE_PIPELINE_RESUME_BUDGET",         "pipeline_resume_budget",         _int_floor(0),     True),
    # Forge layer (issue #297) — current knobs, no deprecation. Validation
    # of ``forge_kind`` is loose here (raw lowercase string) and tightened
    # in :func:`deile.orchestration.forge.detection.detect_forge_kind`,
    # which is the only consumer that needs to reject typos.
    ("DEILE_FORGE_REPO",                     "forge_repo",                     str,               False),
    ("DEILE_FORGE_KIND",                     "forge_kind",                     lambda s: s.strip().lower(), False),
    ("DEILE_GITHUB_HOST",                    "forge_github_host",              str,               False),
    ("DEILE_GITLAB_HOST",                    "forge_gitlab_host",              str,               False),
    ("DEILE_FORGE_PROBE",                    "forge_probe_enabled",            _env_bool,         False),
    ("DEILE_FORGE_BOT_LOGIN",                "forge_bot_login",                str,               False),
    ("DEILE_GITLAB_API_VERSION",             "forge_gitlab_api_version",       str,               False),
    ("DEILE_GITHUB_API_PREFIX",              "forge_github_api_prefix",        str,               False),
    ("DEILE_CRON_DB_PATH",                   "cron_db_path",                   _resolved_path,    True),
    ("DEILE_CRON_POLL_INTERVAL",             "cron_poll_interval",             int,               True),
    # Current knob (no deprecation): agent tool-loop cap.
    ("DEILE_MAX_TOOL_ITERATIONS",            "max_tool_iterations",            _int_floor(1),     False),
    # Sub-DEILEs paralelos (issue #257) — current knobs, no deprecation.
    ("DEILE_SUBAGENT_RUNNER",                "subagent_runner",                lambda s: s.strip().lower(), False),
    ("DEILE_SUBAGENT_MAX_PARALLEL",          "subagent_max_parallel",          _int_floor(1),     False),
    ("DEILE_SUBAGENT_POLL_INTERVAL_S",       "subagent_poll_interval_s",       float,             False),
    ("DEILE_SUBAGENT_BUDGET_S",              "subagent_budget_s",              float,             False),
    ("DEILE_SUBAGENT_CAPTURE_BUFFER_MAX_BYTES", "subagent_capture_buffer_max_bytes", _int_floor(1), False),
    # Per-stage model override (issue #305) — cluster path. The panel TUI
    # writes these via ``kubectl set env deploy/deile-worker`` (parallel to
    # ``set_preferred_model``). The CLI local path uses ``pipeline.models.*``
    # in settings.json. Both layers run through ``_to_optional_model_slug``
    # so a malformed slug is dropped with a warning, never silently used.
    ("DEILE_PIPELINE_MODEL_CLASSIFY",        "pipeline_model_classify",        _to_optional_model_slug, False),
    ("DEILE_PIPELINE_MODEL_REFINE",          "pipeline_model_refine",          _to_optional_model_slug, False),
    ("DEILE_PIPELINE_MODEL_IMPLEMENT",       "pipeline_model_implement",       _to_optional_model_slug, False),
    ("DEILE_PIPELINE_MODEL_PR_REVIEW",       "pipeline_model_pr_review",       _to_optional_model_slug, False),
    ("DEILE_PIPELINE_MODEL_FOLLOW_UPS",      "pipeline_model_follow_ups",      _to_optional_model_slug, False),
    # Per-stage dispatcher override (issue #309) — cluster path. Operates em
    # paridade com pipeline.dispatchers.<stage> em settings.json. Validator
    # ``_to_optional_dispatcher`` consulta ``is_valid_dispatcher`` do
    # :mod:`dispatch_resolver` — aceita canônico + aliases legacy de PR #330.
    ("DEILE_PIPELINE_DISPATCH_CLASSIFY",     "pipeline_dispatcher_classify",   _to_optional_dispatcher, False),
    ("DEILE_PIPELINE_DISPATCH_REFINE",       "pipeline_dispatcher_refine",     _to_optional_dispatcher, False),
    ("DEILE_PIPELINE_DISPATCH_IMPLEMENT",    "pipeline_dispatcher_implement",  _to_optional_dispatcher, False),
    ("DEILE_PIPELINE_DISPATCH_PR_REVIEW",    "pipeline_dispatcher_pr_review",  _to_optional_dispatcher, False),
    ("DEILE_PIPELINE_DISPATCH_FOLLOW_UPS",   "pipeline_dispatcher_follow_ups", _to_optional_dispatcher, False),
)


def _apply_env_overrides(settings: "Settings") -> None:
    """Apply DEILE_* env vars on top of JSON settings, with deprecation warnings."""
    env = os.environ.get

    def _warn(var: str, json_key: str) -> None:
        logger.warning(
            "Env var %s is deprecated. Set '%s' in ~/.deile/settings.json instead.",
            var,
            json_key,
        )

    for env_var, attr, convert, deprecated in _ENV_OVERRIDES:
        raw = env(env_var)
        if not raw:
            continue
        if deprecated:
            _warn(env_var, _DEILE_DEPRECATED_ENV_VARS.get(env_var, env_var))
        try:
            setattr(settings, attr, convert(raw))
        except (ValueError, TypeError):
            # Legacy behavior: malformed env values are silently ignored, the
            # default (or JSON layer value) stays in place.
            pass


# ---------------------------------------------------------------------------
# Layered loading helpers (issue #111)
# ---------------------------------------------------------------------------


def _normalize_path_for_comparison(p: Path) -> str:
    """Return a path string suitable for trust-allowlist equality checks.

    P2-2 (issue #125): on case-insensitive filesystems (macOS HFS+ / APFS,
    Windows NTFS) ``/Users/me/Project`` and ``/Users/me/project`` reference
    the same directory, but a naive string compare disagrees. We resolve
    the path (collapsing symlinks and ``..``) and then apply
    ``os.path.normcase`` so the same physical directory always produces
    the same allowlist key, regardless of the case used in the source.
    """
    try:
        resolved = str(p.expanduser().resolve())
    except OSError:
        resolved = str(p)
    return os.path.normcase(resolved)


def _is_project_layer_trusted(
    cwd: Path,
    allowlist: List[str],
    default_policy: str,
) -> Tuple[bool, str]:
    """Decide whether the project layer at *cwd* should be applied.

    Returns ``(trusted, reason)`` where *reason* is a short tag suitable for
    the warning log. The allowlist contains absolute path strings; we compare
    against ``cwd.resolve()`` after running both sides through
    ``os.path.normcase`` so case-insensitive filesystems match correctly
    (P2-2). Symlinks are followed (intentional — the user asked for
    *this directory*, regardless of link traversal).

    Migration knob ``trust.project_layer_default``:
      - ``"auto"`` (default): honor non-allowlisted with a loud warning so
        existing CIs do not break instantly. Will flip to ``"deny"`` in the
        next major release (CHANGELOG).
      - ``"deny"``: ignore non-allowlisted silently (after one warning).

    Any other value is treated as ``"auto"`` so a typo cannot lock the user
    out of their own project.
    """
    # P1-1 backstop: if the allowlist somehow arrived as a string (a config
    # bypass we now reject in ``_set_typed`` but defensive coding pays here),
    # treat it as empty rather than iterating per-character.
    if not isinstance(allowlist, list):
        logger.warning(
            "settings: trust_project_layer_dirs is not a list (got %s); "
            "treating as empty",
            type(allowlist).__name__,
        )
        allowlist = []

    cwd_key = _normalize_path_for_comparison(cwd)

    normalized_allowlist: List[str] = []
    for entry in allowlist:
        try:
            normalized_allowlist.append(_normalize_path_for_comparison(Path(entry)))
        except OSError:
            normalized_allowlist.append(os.path.normcase(str(entry)))

    if cwd_key in normalized_allowlist:
        return True, "allowlisted"

    if default_policy == "deny":
        return False, "denied_by_policy"
    # "auto" or unknown — honor with a warning; this will flip to "deny" in
    # the next major.
    return True, "auto_grace_period"


def _peek_profile_name(global_path: Path) -> str:
    """Read only ``profile.name`` from the user settings file, without applying
    any other overrides.  Used to resolve the profile YAML before the full
    layered-load so that profile settings are the lowest-priority layer.
    """
    data = _load_json_file(global_path)
    found, value = _resolve_dotted(data, "profile.name")
    return str(value) if found and value else "autonomous_agent"


def _apply_profile_layer(settings: "Settings") -> None:
    """Apply ``deile/config/profiles/{profile_name}.yaml`` as the lowest-priority
    settings layer.  The YAML uses the same nested key structure as the profile
    files (``security.sandbox_code_execution``, etc.) and is processed through
    ``_apply_nested_dict`` → ``_JSON_FIELD_MAP`` so only mapped keys are applied.

    A missing profile file is silently ignored (keeps dataclass defaults).
    """
    profiles_dir = Path(__file__).parent / "profiles"
    profile_path = profiles_dir / f"{settings.profile_name}.yaml"
    if not profile_path.exists():
        logger.debug("settings: profile file not found: %s", profile_path)
        return
    try:
        import yaml  # noqa: PLC0415 — lazy import; PyYAML is a required dep

        data = yaml.safe_load(profile_path.read_text(encoding="utf-8")) or {}
        _apply_nested_dict(settings, data)
        logger.debug("settings: applied profile %s", profile_path.name)
    except Exception as exc:  # pragma: no cover
        logger.warning("settings: cannot apply profile %s: %s", profile_path, exc)


def _apply_user_layer(settings: "Settings", global_path: Path) -> None:
    """Apply ``~/.deile/settings.json`` overrides on top of *settings*.

    Always safe to call — a missing file results in a no-op. Loads the
    trust allowlist as a side effect (consumed by
    :func:`_apply_project_layer_if_trusted`).
    """
    _apply_nested_dict(settings, _load_json_file(global_path))


def _apply_project_layer_if_trusted(
    settings: "Settings", cwd: Path, project_path: Path
) -> None:
    """Conditionally apply ``<cwd>/.deile/settings.json`` overrides.

    Trust-boundary (issue #125): ``<cwd>/.deile/settings.json`` is **not**
    a trusted source. A repo cloned from a third party can carry a
    settings file that disables ``file_safety``, alters ``working_directory``
    or ``debug`` — turning the post-clone ``python deile.py`` into an attack
    vector. The user must opt-in per directory via ``trust.project_layer_dirs``
    in ``~/.deile/settings.json``.
    """
    if not project_path.exists():
        return

    trusted, reason = _is_project_layer_trusted(
        cwd,
        settings.trust_project_layer_dirs,
        settings.trust_project_layer_default,
    )
    if trusted:
        if reason == "auto_grace_period":
            logger.warning(
                "settings: applying project layer %s WITHOUT explicit trust "
                "(cwd not in 'trust.project_layer_dirs' allowlist). This is "
                "the V1 grace-period default; the next major release will "
                "ignore non-allowlisted project layers. Add %s to "
                "'trust.project_layer_dirs' in ~/.deile/settings.json to "
                "silence this warning.",
                project_path,
                str(cwd),
            )
        _apply_nested_dict(settings, _load_json_file(project_path))
    else:
        logger.warning(
            "settings: ignoring project layer %s — cwd %s is not in "
            "'trust.project_layer_dirs' allowlist (policy=%s). Add the "
            "directory to ~/.deile/settings.json to enable.",
            project_path,
            str(cwd),
            settings.trust_project_layer_default,
        )


def _apply_legacy_fallback(cwd: Path) -> Optional["Settings"]:
    """Return ``Settings`` from ``config/settings.json`` if it exists.

    Only used when neither new-layer file (user nor project) is present.
    Returns ``None`` when no legacy file is found, so the caller knows to
    keep the dataclass defaults.
    """
    legacy_path = cwd / "config" / "settings.json"
    if not legacy_path.exists():
        return None
    logger.warning(
        "settings: loading legacy %s; migrate to ~/.deile/settings.json (issue #111)",
        legacy_path,
    )
    return Settings.load_from_file(legacy_path)


def _resolve_global_settings_path() -> Path:
    """Path do JSON do user layer, honrando ``DEILE_SETTINGS_FILE`` se setado.

    Quando rodamos em K8s, o ``~/.deile/`` é um emptyDir writable (precisamos
    pra logs/sessions/run) e não é local pra montar um ConfigMap (mounts de
    subPath em ``~/.deile/`` criam o diretório como ``root:deile 0755``,
    tornando o resto não-writable para o usuário 10001). A saída limpa é
    montar o ConfigMap em ``/etc/deile/settings.json`` e apontar o loader
    pra esse path via env var — preservando o mesmo formato JSON e a
    semântica de camadas. Sem a env var, o comportamento default
    (``~/.deile/settings.json``) é preservado.

    A env var ``DEILE_SETTINGS_FILE`` é **infra-only** (não-deprecada): ela
    apenas relocaliza o user layer; não substitui a migração env-var → JSON
    da issue #111.
    """
    override = os.environ.get("DEILE_SETTINGS_FILE", "").strip()
    if override:
        return Path(override)
    return Path.home() / ".deile" / "settings.json"


def _load_layered_settings() -> "Settings":
    """Build a fresh ``Settings`` from defaults + ``.deile/settings.json`` layers.

    Order:
      1. Defaults from the dataclass.
      2. Apply user layer (``~/.deile/settings.json`` por default, ou o path
         apontado por ``DEILE_SETTINGS_FILE``) — também onde o trust
         allowlist (``trust.project_layer_dirs``) é lido.
      3. Conditionally apply ``<cwd>/.deile/settings.json`` (project)
         overrides on top — only when ``cwd`` is in the allowlist or the
         migration policy is ``"auto"`` (the default). **Pulado quando o
         project_path coincide com o global_path** — evita aplicar o mesmo
         arquivo duas vezes (cenário comum em containers onde HOME == cwd).
      4. Apply DEILE_* env vars as deprecated fallback (win over JSON for
         backward compat).

    If neither layer exists but the legacy ``config/settings.json`` is
    present, fall back to it once with a deprecation warning.
    """
    settings = Settings()
    cwd = Path.cwd()
    global_path = _resolve_global_settings_path()
    project_path = cwd / ".deile" / "settings.json"

    # Layer 0: profile preset — peek at profile.name from user settings first
    # so the right YAML is loaded, then apply it at lowest priority.
    settings.profile_name = _peek_profile_name(global_path)
    _apply_profile_layer(settings)

    # Layer 1: global user preferences — wins over profile
    _apply_user_layer(settings, global_path)

    # Layer 2: project preferences — gated by trust boundary (issue #125).
    # Pula quando o arquivo coincide com o global (HOME == cwd em containers
    # com workingDir == HOME): aplicar o mesmo JSON duas vezes não muda
    # valores mas emite warning de "project layer sem trust".
    try:
        same_file = global_path.exists() and project_path.exists() and (
            global_path.resolve() == project_path.resolve()
        )
    except OSError:
        same_file = False
    if not same_file:
        _apply_project_layer_if_trusted(settings, cwd, project_path)

    # Legacy fallback: if neither new-layer file exists, honor config/settings.json
    if not global_path.exists() and not project_path.exists():
        legacy = _apply_legacy_fallback(cwd)
        if legacy is not None:
            # Still apply env overrides on top so DEILE_* vars always win.
            _apply_env_overrides(legacy)
            return legacy

    # Layer 3: env vars as deprecated fallback (win over JSON for backward compat)
    _apply_env_overrides(settings)

    return settings


# ---------------------------------------------------------------------------
# Singleton
# ---------------------------------------------------------------------------

_settings: Optional["Settings"] = None
_settings_lock = threading.Lock()


def get_settings() -> "Settings":
    """Retorna instância singleton das configurações.

    Reads ``.deile/settings.json`` (project > user > defaults). The legacy
    ``config/settings.json`` is honored as a one-shot fallback when neither
    new-layer file exists. Thread-safe via double-checked locking.
    """
    global _settings
    if _settings is None:
        with _settings_lock:
            if _settings is None:
                _settings = _load_layered_settings()
    return _settings


def update_settings(**kwargs: Any) -> None:
    get_settings().update(**kwargs)


def reset_settings() -> None:
    global _settings
    with _settings_lock:
        _settings = None
