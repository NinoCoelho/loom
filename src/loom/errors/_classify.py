"""Classification pipeline — maps (status, body, exception) → ClassifiedError."""

from __future__ import annotations

import json
from typing import Any

from loom.errors._builder import _build
from loom.errors._extract import (
    extract_error_body,
    extract_error_code,
    extract_message,
    extract_status_code,
)
from loom.errors._messages import _classify_by_error_code, _classify_by_message
from loom.errors._patterns import (
    _AUTH_PATTERNS,
    _BILLING_PATTERNS,
    _CONTEXT_OVERFLOW_PATTERNS,
    _PAYLOAD_TOO_LARGE_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _SERVER_DISCONNECT_PATTERNS,
    _TRANSPORT_ERROR_TYPES,
    _USAGE_LIMIT_PATTERNS,
    _USAGE_LIMIT_TRANSIENT_SIGNALS,
)
from loom.errors._status import _classify_by_status
from loom.errors._types import (
    ClassifiedError,
    FailoverReason,
    LLMError,
    MalformedOutputError,
)


def _classify_402(
    error_msg: str,
    *,
    status: int,
    message: str,
) -> ClassifiedError:
    """402 disambiguation for the HTTP entry point (no exception context)."""
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)

    if has_usage_limit and has_transient_signal:
        return _build(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
            status_code=status,
            message=message,
        )

    return _build(
        FailoverReason.BILLING,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
        status_code=status,
        message=message,
    )


def classify_http(status: int, body: str = "") -> ClassifiedError:
    """Classify by HTTP status + body message."""
    lower = (body or "").lower()

    if any(p in lower for p in _CONTEXT_OVERFLOW_PATTERNS):
        return _build(
            FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
            status_code=status if status else None,
            message=body or "",
        )
    if any(p in lower for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return _build(
            FailoverReason.PAYLOAD_TOO_LARGE,
            retryable=True,
            should_compress=True,
            status_code=status if status else None,
            message=body or "",
        )

    if status == 401:
        return _build(
            FailoverReason.AUTH,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
            status_code=status,
            message=body or "",
        )
    if status == 402:
        return _classify_402(lower, status=status, message=body or "")
    if status == 403:
        if "key limit exceeded" in lower or "spending limit" in lower:
            return _build(
                FailoverReason.BILLING,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
                status_code=status,
                message=body or "",
            )
        return _build(
            FailoverReason.AUTH,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
            status_code=status,
            message=body or "",
        )
    if status == 404:
        return _build(
            FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
            status_code=status,
            message=body or "",
        )
    if status == 413:
        return _build(
            FailoverReason.PAYLOAD_TOO_LARGE,
            retryable=True,
            should_compress=True,
            status_code=status,
            message=body or "",
        )
    if status == 429:
        return _build(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
            status_code=status,
            message=body or "",
        )
    if status in (500, 502):
        return _build(
            FailoverReason.SERVER_ERROR,
            retryable=True,
            status_code=status,
            message=body or "",
        )
    if status in (503, 529):
        return _build(
            FailoverReason.OVERLOADED,
            retryable=True,
            status_code=status,
            message=body or "",
        )
    if 500 <= status < 600:
        return _build(
            FailoverReason.SERVER_ERROR,
            retryable=True,
            status_code=status,
            message=body or "",
        )
    if 400 <= status < 500:
        if any(p in lower for p in _AUTH_PATTERNS):
            return _build(
                FailoverReason.AUTH,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
                status_code=status,
                message=body or "",
            )
        if any(p in lower for p in _BILLING_PATTERNS):
            return _build(
                FailoverReason.BILLING,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
                status_code=status,
                message=body or "",
            )
        if any(p in lower for p in _RATE_LIMIT_PATTERNS):
            return _build(
                FailoverReason.RATE_LIMIT,
                retryable=True,
                should_rotate_credential=True,
                should_fallback=True,
                status_code=status,
                message=body or "",
            )
        return _build(
            FailoverReason.FORMAT_ERROR,
            retryable=False,
            should_fallback=True,
            status_code=status,
            message=body or "",
        )

    return _build(FailoverReason.UNKNOWN, status_code=status or None, message=body or "")


def _extract_error_messages(error: Exception, body: Any) -> tuple[str, str]:
    """Build the combined error message and provider message-body string."""
    raw_msg = str(error).lower()
    body_msg = ""
    metadata_msg = ""
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            body_msg = (err_obj.get("message") or "").lower()
            metadata = err_obj.get("metadata", {})
            if isinstance(metadata, dict):
                raw_json = metadata.get("raw") or ""
                if isinstance(raw_json, str) and raw_json.strip():
                    try:
                        inner = json.loads(raw_json)
                        if isinstance(inner, dict):
                            inner_err = inner.get("error", {})
                            if isinstance(inner_err, dict):
                                metadata_msg = (inner_err.get("message") or "").lower()
                    except (json.JSONDecodeError, TypeError):
                        pass
        if not body_msg:
            body_msg = (body.get("message") or "").lower()

    parts = [raw_msg]
    if body_msg and body_msg not in raw_msg:
        parts.append(body_msg)
    if metadata_msg and metadata_msg not in raw_msg and metadata_msg not in body_msg:
        parts.append(metadata_msg)
    return " ".join(parts), body_msg


def classify_api_error(
    error: Exception,
    *,
    provider: str = "",
    model: str = "",
    approx_tokens: int = 0,
    context_length: int = 200_000,
    num_messages: int = 0,
) -> ClassifiedError:
    """Classify a raised exception into a recovery recommendation."""
    if isinstance(error, MalformedOutputError):
        return _build(
            FailoverReason.FORMAT_ERROR,
            retryable=False,
            provider=provider or None,
            model=model or None,
            message=str(error),
        )

    status_code = extract_status_code(error)
    error_type = type(error).__name__
    body = extract_error_body(error)
    error_code = extract_error_code(body)

    error_msg, _ = _extract_error_messages(error, body)
    provider_lower = (provider or "").strip().lower()
    model_lower = (model or "").strip().lower()

    def _result(reason: FailoverReason, **overrides) -> ClassifiedError:
        defaults = dict(
            status_code=status_code,
            provider=provider or None,
            model=model or None,
            message=extract_message(error, body),
        )
        defaults.update(overrides)
        return _build(reason, **defaults)

    # 1. Provider-specific patterns
    if status_code == 400 and "signature" in error_msg and "thinking" in error_msg:
        return _result(FailoverReason.THINKING_SIGNATURE, retryable=True)

    if status_code == 429 and "extra usage" in error_msg and "long context" in error_msg:
        return _result(
            FailoverReason.LONG_CONTEXT_TIER,
            retryable=True,
            should_compress=True,
        )

    # 2. HTTP status code classification
    if status_code is not None:
        classified = _classify_by_status(
            status_code,
            error_msg,
            error_code,
            body,
            provider=provider_lower,
            model=model_lower,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=_result,
        )
        if classified is not None:
            return classified

    # 3. Error code classification
    if error_code:
        classified = _classify_by_error_code(error_code, error_msg, _result)
        if classified is not None:
            return classified

    # 4. Message pattern matching
    classified = _classify_by_message(
        error_msg,
        error_type,
        approx_tokens=approx_tokens,
        context_length=context_length,
        result_fn=_result,
    )
    if classified is not None:
        return classified

    # 5. Server disconnect + large session → context overflow
    is_disconnect = any(p in error_msg for p in _SERVER_DISCONNECT_PATTERNS)
    if is_disconnect and not status_code:
        is_large = (
            approx_tokens > context_length * 0.6 or approx_tokens > 120_000 or num_messages > 200
        )
        if is_large:
            return _result(
                FailoverReason.CONTEXT_OVERFLOW,
                retryable=True,
                should_compress=True,
            )
        return _result(FailoverReason.TIMEOUT, retryable=True)

    # 6. Transport / timeout
    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(
        error, (TimeoutError, ConnectionError, OSError)
    ):
        return _result(FailoverReason.TIMEOUT, retryable=True)

    if isinstance(error, LLMError):
        return _result(FailoverReason.UNKNOWN, retryable=True)

    # 7. Fallback
    return _result(FailoverReason.UNKNOWN, retryable=True)
