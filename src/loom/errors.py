from __future__ import annotations

import re
from enum import Enum
from typing import Any

from pydantic import BaseModel


class FailoverReason(str, Enum):
    AUTH = "auth"
    BILLING = "billing"
    RATE_LIMIT = "rate_limit"
    OVERLOADED = "overloaded"
    SERVER_ERROR = "server_error"
    TIMEOUT = "timeout"
    CONTEXT_OVERFLOW = "context_overflow"
    PAYLOAD_TOO_LARGE = "payload_too_large"
    MODEL_NOT_FOUND = "model_not_found"
    FORMAT_ERROR = "format_error"
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    ROTATE_CREDENTIAL = "rotate_credential"
    COMPRESS = "compress"
    ABORT = "abort"


class LLMError(Exception):
    pass


class LLMTransportError(LLMError):
    def __init__(
        self,
        message: str,
        status_code: int | None = None,
        body: str | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.body = body


class MalformedOutputError(LLMError):
    pass


class ClassifiedError(BaseModel):
    reason: FailoverReason
    retryable: bool
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    recovery: RecoveryAction


_REASON_MAP: dict[FailoverReason, RecoveryAction] = {
    FailoverReason.AUTH: RecoveryAction.ROTATE_CREDENTIAL,
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
    FailoverReason.UNKNOWN: RecoveryAction.RETRY_AFTER_BACKOFF,
}

_BODY_PATTERNS: list[tuple[re.Pattern[str], FailoverReason]] = [
    (re.compile(r"invalid.?api.?key|unauthorized|authentication", re.I), FailoverReason.AUTH),
    (re.compile(r"billing|quota|capacity", re.I), FailoverReason.BILLING),
    (re.compile(r"rate.?limit|too.?many.?requests", re.I), FailoverReason.RATE_LIMIT),
    (re.compile(r"overloaded|capacity", re.I), FailoverReason.OVERLOADED),
    (re.compile(r"timeout|timed.?out", re.I), FailoverReason.TIMEOUT),
    (re.compile(r"context.?length|max.?tokens|too.?many.?tokens", re.I), FailoverReason.CONTEXT_OVERFLOW),
    (re.compile(r"model.?not.?found", re.I), FailoverReason.MODEL_NOT_FOUND),
    (re.compile(r"invalid|malformed|unexpected.?format", re.I), FailoverReason.FORMAT_ERROR),
]


def classify_http(status: int, body: str) -> ClassifiedError:
    if body:
        for pat, reason in _BODY_PATTERNS:
            if pat.search(body):
                return _build(reason)

    if status == 401:
        return _build(FailoverReason.AUTH)
    if status == 402:
        return _build(FailoverReason.BILLING)
    if status == 403:
        return _build(FailoverReason.AUTH)
    if status == 404:
        return _build(FailoverReason.MODEL_NOT_FOUND)
    if status == 413:
        return _build(FailoverReason.PAYLOAD_TOO_LARGE)
    if status == 429:
        return _build(FailoverReason.RATE_LIMIT)
    if 500 <= status < 600:
        return _build(FailoverReason.SERVER_ERROR)

    return _build(FailoverReason.UNKNOWN)


def classify_api_error(error: Exception) -> ClassifiedError:
    if isinstance(error, MalformedOutputError):
        return ClassifiedError(
            reason=FailoverReason.FORMAT_ERROR,
            retryable=False,
            recovery=RecoveryAction.ABORT,
        )
    if isinstance(error, LLMTransportError):
        status = error.status_code
        body = error.body or ""
        if status is not None:
            return classify_http(status, body)
    if isinstance(error, LLMError):
        return _build(FailoverReason.UNKNOWN)
    return _build(FailoverReason.UNKNOWN)


def _build(reason: FailoverReason) -> ClassifiedError:
    recovery = _REASON_MAP.get(reason, RecoveryAction.RETRY_AFTER_BACKOFF)
    return ClassifiedError(
        reason=reason,
        retryable=recovery in (RecoveryAction.RETRY, RecoveryAction.RETRY_AFTER_BACKOFF),
        should_compress=reason in (FailoverReason.CONTEXT_OVERFLOW, FailoverReason.PAYLOAD_TOO_LARGE),
        should_rotate_credential=reason == FailoverReason.AUTH,
        should_fallback=reason in (FailoverReason.OVERLOADED, FailoverReason.CONTEXT_OVERFLOW),
        recovery=recovery,
    )
