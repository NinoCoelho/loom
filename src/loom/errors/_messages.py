"""Error-code and message-pattern classifiers."""

from __future__ import annotations

from collections.abc import Callable

from loom.errors._patterns import (
    _AUTH_PATTERNS,
    _BILLING_PATTERNS,
    _CONTEXT_OVERFLOW_PATTERNS,
    _MODEL_NOT_FOUND_PATTERNS,
    _PAYLOAD_TOO_LARGE_PATTERNS,
    _RATE_LIMIT_PATTERNS,
    _USAGE_LIMIT_PATTERNS,
    _USAGE_LIMIT_TRANSIENT_SIGNALS,
)
from loom.errors._types import ClassifiedError, FailoverReason


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
