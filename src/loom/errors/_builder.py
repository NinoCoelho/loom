"""ClassifiedError construction helpers — recovery map + `_build`."""

from __future__ import annotations

from typing import Any

from loom.errors._types import ClassifiedError, FailoverReason, RecoveryAction

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


def _derive_recovery(reason: FailoverReason) -> RecoveryAction:
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
    return ClassifiedError(
        reason=reason,
        retryable=retryable,
        should_compress=should_compress,
        should_rotate_credential=should_rotate_credential,
        should_fallback=should_fallback,
        recovery=_derive_recovery(reason),
        status_code=status_code,
        provider=provider,
        model=model,
        message=message,
        error_context=error_context or {},
    )
