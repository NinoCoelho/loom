"""Classification pipeline — maps (status, body, exception) → ClassifiedError."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from loom.errors._extract import (
    extract_error_body,
    extract_error_code,
    extract_message,
    extract_status_code,
)
from loom.errors._patterns import (
    _AUTH_PATTERNS,
    _BILLING_PATTERNS,
    _CONTEXT_OVERFLOW_PATTERNS,
    _MODEL_NOT_FOUND_PATTERNS,
    _PAYLOAD_TOO_LARGE_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _SERVER_DISCONNECT_PATTERNS,
    _TRANSPORT_ERROR_TYPES,
    _USAGE_LIMIT_PATTERNS,
    _USAGE_LIMIT_TRANSIENT_SIGNALS,
)
from loom.errors._types import (
    ClassifiedError,
    FailoverReason,
    LLMError,
    MalformedOutputError,
    RecoveryAction,
)

# ── Recovery action mapping ─────────────────────────────────────────────

_RECOVERY_MAP: dict[FailoverReason, RecoveryAction] = {
    FailoverReason.AUTH: RecoveryAction.ROTATE_CREDENTIAL,
    FailoverReason.AUTH_PERMANENT: RecoveryAction.ABORT,
    FailoverReason.BILLING: RecoveryAction.ABORT,
    FailoverReason.RATE_LIMIT: RecoveryAction.RETRY_AFTER_BACKOFF,
    FailoverReason.OVERLOADED: RecoveryAction.RETRY_AFTER_BACKOFF,
    FailoverReason.SERVER_ERROR: RecoveryAction.RETRY_AFTER_BACKOFF,
    FailoverReason.TIMEOUT: RecoveryAction.RETRY,
    FailoverReason.CONTEXT_OVERFLOW: RecoveryAction.COMPRESS,
    FailoverReason.PAYLOAD_TOO_LARGE: RecoveryAction.COMPRESS,
    FailoverReason.MODEL_NOT_FOUND: RecoveryAction.ABORT,
    FailoverReason.FORMAT_ERROR: RecoveryAction.ABORT,
    FailoverReason.BAD_REQUEST: RecoveryAction.ABORT,
    FailoverReason.NOT_FOUND: RecoveryAction.ABORT,
    FailoverReason.THINKING_SIGNATURE: RecoveryAction.RETRY_AFTER_BACKOFF,
    FailoverReason.LONG_CONTEXT_TIER: RecoveryAction.COMPRESS,
    FailoverReason.UNKNOWN: RecoveryAction.RETRY_AFTER_BACKOFF,
}


def _derive_recovery(
    reason: FailoverReason,
) -> RecoveryAction:
    return _RECOVERY_MAP.get(reason, RecoveryAction.RETRY_AFTER_BACKOFF)


def _build(
    reason: FailoverReason,
    *,
    retryable: bool | None = None,
    should_compress: bool = False,
    should_rotate_credential: bool = False,
    should_fallback: bool = False,
    status_code: int | None = None,
    provider: str | None = None,
    model: str | None = None,
    message: str = "",
    error_context: dict[str, Any] | None = None,
) -> ClassifiedError:
    if retryable is None:
        retryable = _RECOVERY_MAP.get(reason, RecoveryAction.RETRY_AFTER_BACKOFF) in (
            RecoveryAction.RETRY,
            RecoveryAction.RETRY_AFTER_BACKOFF,
        )
    recovery = _derive_recovery(reason)
    return ClassifiedError(
        reason=reason,
        retryable=retryable,
        should_compress=should_compress,
        should_rotate_credential=should_rotate_credential,
        should_fallback=should_fallback,
        recovery=recovery,
        status_code=status_code,
        provider=provider,
        model=model,
        message=message,
        error_context=error_context or {},
    )


# ── 402 disambiguation (shared by classify_http and _classify_by_status) ──


def _classify_402(
    error_msg: str,
    *,
    status: int,
    message: str,
) -> ClassifiedError:
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


def _classify_402_from(
    error_msg: str,
    result_fn: Callable[..., ClassifiedError],
) -> ClassifiedError:
    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)

    if has_usage_limit and has_transient_signal:
        return result_fn(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )
    return result_fn(
        FailoverReason.BILLING,
        retryable=False,
        should_rotate_credential=True,
        should_fallback=True,
    )


# ── Status code classification ──────────────────────────────────────────


def _classify_by_status(
    status_code: int,
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    provider: str,
    model: str,
    approx_tokens: int,
    context_length: int,
    num_messages: int,
    result_fn: Callable[..., ClassifiedError],
) -> ClassifiedError | None:
    if status_code == 401:
        return result_fn(
            FailoverReason.AUTH,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 403:
        if "key limit exceeded" in error_msg or "spending limit" in error_msg:
            return result_fn(
                FailoverReason.BILLING,
                retryable=False,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.AUTH,
            retryable=False,
            should_fallback=True,
        )

    if status_code == 402:
        return _classify_402_from(error_msg, result_fn)

    if status_code == 404:
        return result_fn(
            FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
        )

    if status_code == 413:
        return result_fn(
            FailoverReason.PAYLOAD_TOO_LARGE,
            retryable=True,
            should_compress=True,
        )

    if status_code == 429:
        return result_fn(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if status_code == 400:
        return _classify_400(
            error_msg,
            error_code,
            body,
            provider=provider,
            model=model,
            approx_tokens=approx_tokens,
            context_length=context_length,
            num_messages=num_messages,
            result_fn=result_fn,
        )

    if status_code in (500, 502):
        return result_fn(FailoverReason.SERVER_ERROR, retryable=True)

    if status_code in (503, 529):
        return result_fn(FailoverReason.OVERLOADED, retryable=True)

    if 400 <= status_code < 500:
        return result_fn(
            FailoverReason.FORMAT_ERROR,
            retryable=False,
            should_fallback=True,
        )

    if 500 <= status_code < 600:
        return result_fn(FailoverReason.SERVER_ERROR, retryable=True)

    return None


def _classify_400(
    error_msg: str,
    error_code: str,
    body: dict,
    *,
    provider: str,
    model: str,
    approx_tokens: int,
    context_length: int,
    num_messages: int,
    result_fn: Callable[..., ClassifiedError],
) -> ClassifiedError:
    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
        )

    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
        )

    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )
    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.BILLING,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    err_body_msg = ""
    if isinstance(body, dict):
        err_obj = body.get("error", {})
        if isinstance(err_obj, dict):
            err_body_msg = (err_obj.get("message") or "").strip().lower()
        if not err_body_msg:
            err_body_msg = (body.get("message") or "").strip().lower()
    is_generic = len(err_body_msg) < 30 or err_body_msg in ("error", "")
    is_large = approx_tokens > context_length * 0.4 or approx_tokens > 80_000 or num_messages > 80

    if is_generic and is_large:
        return result_fn(
            FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
        )

    return result_fn(
        FailoverReason.FORMAT_ERROR,
        retryable=False,
        should_fallback=True,
    )


# ── Error code classification ───────────────────────────────────────────


def _classify_by_error_code(
    error_code: str,
    error_msg: str,
    result_fn: Callable[..., ClassifiedError],
) -> ClassifiedError | None:
    code_lower = error_code.lower()

    if code_lower in ("resource_exhausted", "throttled", "rate_limit_exceeded"):
        return result_fn(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
        )

    if code_lower in ("insufficient_quota", "billing_not_active", "payment_required"):
        return result_fn(
            FailoverReason.BILLING,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if code_lower in ("model_not_found", "model_not_available", "invalid_model"):
        return result_fn(
            FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
        )

    if code_lower in ("context_length_exceeded", "max_tokens_exceeded"):
        return result_fn(
            FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
        )

    return None


# ── Message pattern classification ──────────────────────────────────────


def _classify_by_message(
    error_msg: str,
    error_type: str,
    *,
    approx_tokens: int,
    context_length: int,
    result_fn: Callable[..., ClassifiedError],
) -> ClassifiedError | None:
    if any(p in error_msg for p in _PAYLOAD_TOO_LARGE_PATTERNS):
        return result_fn(
            FailoverReason.PAYLOAD_TOO_LARGE,
            retryable=True,
            should_compress=True,
        )

    has_usage_limit = any(p in error_msg for p in _USAGE_LIMIT_PATTERNS)
    if has_usage_limit:
        has_transient_signal = any(p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS)
        if has_transient_signal:
            return result_fn(
                FailoverReason.RATE_LIMIT,
                retryable=True,
                should_rotate_credential=True,
                should_fallback=True,
            )
        return result_fn(
            FailoverReason.BILLING,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _BILLING_PATTERNS):
        return result_fn(
            FailoverReason.BILLING,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _RATE_LIMIT_PATTERNS):
        return result_fn(
            FailoverReason.RATE_LIMIT,
            retryable=True,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _CONTEXT_OVERFLOW_PATTERNS):
        return result_fn(
            FailoverReason.CONTEXT_OVERFLOW,
            retryable=True,
            should_compress=True,
        )

    if any(p in error_msg for p in _AUTH_PATTERNS):
        return result_fn(
            FailoverReason.AUTH,
            retryable=False,
            should_rotate_credential=True,
            should_fallback=True,
        )

    if any(p in error_msg for p in _MODEL_NOT_FOUND_PATTERNS):
        return result_fn(
            FailoverReason.MODEL_NOT_FOUND,
            retryable=False,
            should_fallback=True,
        )

    return None


# ── Public API ──────────────────────────────────────────────────────────


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
    error_msg = " ".join(parts)
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

    # ── 1. Provider-specific patterns ───────────────────────────────
    if status_code == 400 and "signature" in error_msg and "thinking" in error_msg:
        return _result(FailoverReason.THINKING_SIGNATURE, retryable=True)

    if status_code == 429 and "extra usage" in error_msg and "long context" in error_msg:
        return _result(
            FailoverReason.LONG_CONTEXT_TIER,
            retryable=True,
            should_compress=True,
        )

    # ── 2. HTTP status code classification ──────────────────────────
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

    # ── 3. Error code classification ────────────────────────────────
    if error_code:
        classified = _classify_by_error_code(error_code, error_msg, _result)
        if classified is not None:
            return classified

    # ── 4. Message pattern matching ─────────────────────────────────
    classified = _classify_by_message(
        error_msg,
        error_type,
        approx_tokens=approx_tokens,
        context_length=context_length,
        result_fn=_result,
    )
    if classified is not None:
        return classified

    # ── 5. Server disconnect + large session → context overflow ─────
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

    # ── 6. Transport / timeout ──────────────────────────────────────
    if error_type in _TRANSPORT_ERROR_TYPES or isinstance(
        error, (TimeoutError, ConnectionError, OSError)
    ):
        return _result(FailoverReason.TIMEOUT, retryable=True)

    if isinstance(error, LLMError):
        return _result(FailoverReason.UNKNOWN, retryable=True)

    # ── 7. Fallback ─────────────────────────────────────────────────
    return _result(FailoverReason.UNKNOWN, retryable=True)
