"""Security helpers for ``SettingsManager`` writes (issue #125).

Extracted from ``settings_manager.py`` so the manager body stays focused on
JSON persistence while permission / audit / fingerprinting concerns live
together. All functions here are best-effort: they must never block a
caller, but they may refuse a write or return a denial verdict.

S-3 / S-4 from the reviewer:
  - The fingerprint helpers are intentionally NOT shared with
    ``deile/security/secrets_scanner.py`` — that module is a regex-based
    scanner over file contents and does not currently produce SHA-256
    fingerprints. If/when ``secrets_scanner`` grows that need, unify here.
  - The defensive ``try/except ImportError`` on intra-package imports of
    ``deile.security.permissions`` and ``deile.security.audit_logger`` was
    intentionally dropped: those modules ship in the same wheel; an
    ImportError there means a broken install, which we let bubble up.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict

# Top-level import (S-1): both modules ship in the same wheel; an
# ImportError here means a broken install, which we let bubble up rather
# than silently fail-open. The runtime call to ``get_permission_manager``
# remains wrapped in defensive try/except below.
from deile.security.permissions import get_permission_manager

logger = logging.getLogger(__name__)

# Secret-looking key patterns — refuse to store these in settings files.
_SECRET_KEY_PATTERNS = ("token", "key", "secret", "password", "api_")


def is_secret_key(key_path: str) -> bool:
    """Return True if *key_path* matches the secret-key blocklist."""
    key_lower = key_path.lower()
    return any(pat in key_lower for pat in _SECRET_KEY_PATTERNS)


def hash_value(value: Any) -> str:
    """Return a SHA-256 truncated hex digest of *value*'s JSON form.

    The raw value is never logged. For values that are not JSON-serializable
    we fall back to ``repr()`` — still no leakage of secrets because callers
    redact secret keys before this function is reached.
    """
    try:
        encoded = json.dumps(value, sort_keys=True, default=str).encode("utf-8")
    except (TypeError, ValueError):
        encoded = repr(value).encode("utf-8", errors="replace")
    return hashlib.sha256(encoded).hexdigest()[:16]


def value_fingerprint(key_path: str, value: Any) -> str:
    """Return ``"<redacted>"`` for secret keys, else a SHA-256 truncated hash.

    ``None`` (no previous value) maps to ``"<absent>"`` so audit consumers
    can distinguish "first write" from "value mutated".
    """
    if value is None:
        return "<absent>"
    if is_secret_key(key_path):
        return "<redacted>"
    return hash_value(value)


def check_settings_write_permission(scope: str, resource_detail: str) -> bool:
    """Resolve the permission manager and check write access.

    Fail-CLOSED contract (issue #125, reviewer P1-5): if any unexpected
    runtime error occurs while consulting the permission manager, denial
    is assumed. The default rule set in ``permissions.py`` is now ``READ``
    (not ``WRITE``) so operators must opt-in to settings writes via
    ``config/permissions.yaml``.

    The resource string follows the convention ``"settings:<scope>:<detail>"``
    so rule authors can scope policies tighter than "any settings write".
    """
    try:
        pm = get_permission_manager()
    except Exception:  # defensive against PermissionManager bugs
        logger.exception(
            "settings: cannot resolve PermissionManager; denying write (fail-closed)"
        )
        return False
    if pm is None:
        # No manager registered — fail-closed (issue #125 P1-5).
        logger.warning(
            "settings: no PermissionManager registered; denying write (fail-closed)"
        )
        return False
    resource = f"settings:{scope}:{resource_detail}"
    try:
        return bool(
            pm.check_permission(
                tool_name="settings_manager",
                resource=resource,
                action="write",
                context={"scope": scope, "detail": resource_detail},
            )
        )
    except Exception:  # defensive against PermissionManager bugs
        logger.exception("settings: PermissionManager.check_permission raised; denying")
        return False


def emit_settings_audit(
    *,
    scope: str,
    resource_detail: str,
    action: str,
    result: str,
    details: Dict[str, Any],
) -> None:
    """Emit a typed ``AuditEvent`` for a settings.json mutation (best-effort).

    Uses :class:`AuditEventType.SECURITY_POLICY_CHANGED` because changes to
    flags like ``file_safety.enabled`` shift the security posture of the
    process. Failures to emit (logger missing, I/O error) are swallowed —
    they must never block the caller.
    """
    try:
        from deile.security.audit_logger import (
            AuditEventType,
            SeverityLevel,
            get_audit_logger,
        )

        logger_obj = get_audit_logger()
        severity = SeverityLevel.INFO if result == "allowed" else SeverityLevel.WARNING
        logger_obj.log_event(
            event_type=AuditEventType.SECURITY_POLICY_CHANGED,
            severity=severity,
            actor="settings_manager",
            resource=f"settings:{scope}:{resource_detail}",
            action=action,
            result=result,
            details=details,
        )
    except Exception:  # audit emit must never block the caller
        logger.exception("settings: audit emission failed")


def validate_against_override_handlers(key_path: str, value: Any):
    """Dry-run validate *value* against ``_OVERRIDE_HANDLERS`` for *key_path*.

    Returns ``None`` when validation passes (or the key has no handler — the
    handler set is intentionally a subset of valid keys). Returns a
    sanitized error string when the converter rejects the value, so the
    caller can refuse the write before touching disk.

    P0-1: the returned string is sanitized to remove the raw *value* before
    it can reach an audit payload — the converter's ``ValueError`` may
    contain ``repr(value)`` (e.g. ``"'<SEGREDO>' is not a valid LogLevel"``).
    """
    from deile.config.settings import _OVERRIDE_HANDLERS

    handler = _OVERRIDE_HANDLERS.get(key_path)
    if handler is None:
        return None
    _field_name, converter = handler
    try:
        converter(value)
    except (TypeError, ValueError) as exc:
        return _sanitize_validation_error(exc, value)
    return None


def _sanitize_validation_error(exc: Exception, value: Any) -> str:
    """Return an error message safe to embed in audit / logs.

    Strips occurrences of the raw *value* (and its ``repr()`` form) — the
    converter functions in ``deile/config/settings.py`` echo back the
    rejected value in their messages, which becomes a leak vector when the
    caller passed a secret-shaped value to a non-secret-shaped key
    (e.g. ``set_setting('logging.level', '<SEGREDO>')``).
    """
    raw = str(exc)
    # Replace both raw and repr forms; clamp at a small length to bound
    # the audit payload regardless of upstream message verbosity.
    for needle in (str(value), repr(value)):
        if needle and needle in raw:
            raw = raw.replace(needle, "<value>")
    if len(raw) > 200:
        raw = raw[:197] + "..."
    return raw
