"""Shared error-envelope builder for concrete LLM providers.

Provider-agnostic: must NOT import any external SDK. Concrete providers
(``anthropic_provider``, ``openai_provider``, …) pass a ``classify_fn``
callable that maps their own exception type to an ``error_type`` string.
"""

from __future__ import annotations

import json
import time
from typing import Any, Callable, Dict, Iterable, Tuple

from deile.core.models.errors import ProviderErrorEnvelope

# Substrings that, when found in an HTTP error message, indicate the request
# exceeded the model's context window. Shared by every provider classifier.
_CONTEXT_LENGTH_MSG_MARKERS = ("maximum context length", "context window")


def classify_http_error(
    status: int | None,
    err_code: str,
    err_msg: str,
    extra_msg_markers: Iterable[str] = (),
) -> str:
    """Classify an HTTP error into a provider-agnostic ``error_type`` string.

    This is the single shared classifier for every concrete provider. Callers
    are responsible for extracting ``status``, ``err_code`` and ``err_msg``
    from their own SDK exception/body structure (Anthropic and OpenAI nest
    these differently) and then delegate the status/sniff logic here.

    Args:
        status: HTTP status code, or ``None`` when unavailable.
        err_code: Provider-specific error code/type, already lower-cased.
        err_msg: Error message, already lower-cased.
        extra_msg_markers: Optional provider-specific message substrings that
            also indicate a context-length overflow (e.g. Anthropic's
            ``"prompt is too long"``).

    Returns:
        One of ``auth``, ``rate_limit``, ``context_length_exceeded``,
        ``invalid_request``, ``server`` or ``unknown``.
    """
    if status == 401:
        return "auth"
    if status == 429:
        return "rate_limit"
    if status and 400 <= status < 500:
        markers = (*_CONTEXT_LENGTH_MSG_MARKERS, *extra_msg_markers)
        if (
            "context_length_exceeded" in err_code
            or "prompt_too_long" in err_code
            or any(marker in err_msg for marker in markers)
        ):
            return "context_length_exceeded"
        return "invalid_request"
    if status and status >= 500:
        return "server"
    return "unknown"


def classify_provider_error(
    exc: Exception,
    body_extractor: Callable[[Dict[str, Any], Exception], Tuple[str, str]],
    extra_msg_markers: Iterable[str] = (),
) -> str:
    """Classify any provider SDK exception into an ``error_type`` string.

    This owns the boilerplate every concrete provider classifier used to
    repeat: reading ``status_code``/``body`` off the exception, lower-casing
    the extracted code/message and delegating the status/sniff logic to
    :func:`classify_http_error`. The only provider-specific knowledge — *where*
    the error code and message live inside the body — is supplied by
    ``body_extractor``.

    Args:
        exc: The provider SDK exception.
        body_extractor: Callable receiving the ``body`` dict and the original
            exception, returning a ``(err_code, err_msg)`` tuple. It is only
            invoked when ``body`` is a ``dict``; otherwise the code is empty
            and the message defaults to ``str(exc)``. Receiving ``exc`` lets a
            provider fall back to ``str(exc)`` when its body carries no message.
        extra_msg_markers: Provider-specific context-length message markers
            forwarded to :func:`classify_http_error`.

    Returns:
        The ``error_type`` string. Exceptions without an HTTP status (i.e.
        not ``APIStatusError``/``APIError``) naturally classify as ``unknown``.
    """
    status = getattr(exc, "status_code", None)
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err_code, err_msg = body_extractor(body, exc)
    else:
        err_code, err_msg = "", str(exc)
    return classify_http_error(
        status,
        str(err_code or "").lower(),
        str(err_msg or "").lower(),
        extra_msg_markers=extra_msg_markers,
    )


def build_error_envelope(
    exc: Exception,
    provider_id: str,
    model_id: str,
    classify_fn: Callable[[Exception], str],
) -> ProviderErrorEnvelope:
    """Build a :class:`ProviderErrorEnvelope` from a provider SDK exception.

    The common work — extracting the HTTP status, parsing the response body
    (``dict``/``bytes``/``str`` → JSON) and resolving the request id — lives
    here. ``classify_fn`` receives the raw exception and returns the
    provider-specific ``error_type`` classification.
    """
    status = getattr(exc, "status_code", None)
    raw: Dict[str, Any] = {}
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        raw = body
    elif isinstance(body, (str, bytes)):
        try:
            raw = json.loads(body)
        except Exception:
            raw = {"raw_body": str(body)}

    request_id = getattr(exc, "request_id", None)
    if request_id is None:
        response = getattr(exc, "response", None)
        if response is not None:
            request_id = getattr(response, "headers", {}).get("request-id")

    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id=model_id,
        error_type=classify_fn(exc),
        message=str(exc),
        http_status=status,
        raw_json=raw,
        request_id=str(request_id) if request_id else None,
        timestamp=time.time(),
    )


def make_envelope_builder(
    body_extractor: Callable[[Dict[str, Any], Exception], Tuple[str, str]],
    extra_msg_markers: Iterable[str] = (),
) -> Callable[[Exception, str, str], ProviderErrorEnvelope]:
    """Build a provider's ``_make_envelope`` callable from its two knobs.

    Concrete providers that follow the standard HTTP-error shape (Anthropic,
    OpenAI) differ only in *where* the error code/message live inside the SDK
    error body and, optionally, an extra context-length message marker. This
    factory captures those two knobs and returns a ready-to-use
    ``(exc, provider_id, model_id) -> ProviderErrorEnvelope`` callable,
    removing the ``_classify_*``/``_make_envelope`` boilerplate each provider
    used to repeat. Providers with a non-standard body layout (e.g. Gemini)
    keep their own builder.
    """
    def _classify(exc: Exception) -> str:
        return classify_provider_error(
            exc, body_extractor, extra_msg_markers=extra_msg_markers
        )

    def _build(
        exc: Exception, provider_id: str, model_id: str
    ) -> ProviderErrorEnvelope:
        return build_error_envelope(exc, provider_id, model_id, _classify)

    return _build


def classify_gemini_error(exc: Exception) -> str:
    """Classify a Google GenAI SDK exception into an ``error_type`` string.

    The ``google-genai`` SDK surfaces API failures as ``APIError`` (and its
    ``ClientError`` / ``ServerError`` subclasses). Unlike Anthropic/OpenAI,
    those exceptions expose the HTTP status as ``code`` (int), a coarse string
    under ``status`` (e.g. ``RESOURCE_EXHAUSTED``) and the human message under
    ``message``. Those fields are read by duck typing — keeping this module
    SDK-free — and the status/sniff logic delegated to
    :func:`classify_http_error`, so Gemini lands on the same ``error_type``
    vocabulary as the other providers.
    """
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = None
    err_code = str(getattr(exc, "status", "") or "").lower()
    err_msg = str(getattr(exc, "message", "") or str(exc) or "").lower()
    return classify_http_error(status, err_code, err_msg)


def make_gemini_envelope(
    exc: Exception, provider_id: str, model_id: str
) -> ProviderErrorEnvelope:
    """Build a typed :class:`ProviderErrorEnvelope` from a GenAI SDK exception.

    Built directly (not via :func:`build_error_envelope`) because GenAI
    exceptions store their HTTP status under ``code`` and the response body
    under ``details`` — a different layout from the Anthropic/OpenAI SDKs that
    helper targets.
    """
    status = getattr(exc, "code", None)
    if not isinstance(status, int):
        status = None
    details = getattr(exc, "details", None)
    raw: Dict[str, Any] = details if isinstance(details, dict) else {}
    message = str(getattr(exc, "message", "") or "") or str(exc)
    return ProviderErrorEnvelope(
        provider_id=provider_id,
        model_id=model_id,
        error_type=classify_gemini_error(exc),
        message=message,
        http_status=status,
        raw_json=raw,
    )
