"""HTTP status-code classifiers (400, 402, general status routing)."""

from __future__ import annotations

from collections.abc import Callable

from loom.errors._patterns import (
    _BILLING_PATTERNS,
    _CONTEXT_OVERFLOW_PATTERNS,
    _MODEL_NOT_FOUND_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _USAGE_LIMIT_PATTERNS,
    _USAGE_LIMIT_TRANSIENT_SIGNALS,
)
from loom.errors._types import ClassifiedError, FailoverReason


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
