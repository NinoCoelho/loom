"""Exception types and classification enums for API error handling."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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


class FailoverReason(StrEnum):
    """Why an LLM call failed — drives recovery strategy."""

    AUTH = "auth"
    AUTH_PERMANENT = "auth_permanent"
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
    THINKING_SIGNATURE = "thinking_signature"
    LONG_CONTEXT_TIER = "long_context_tier"
    UNKNOWN = "unknown"


class RecoveryAction(StrEnum):
    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    ROTATE_CREDENTIAL = "rotate_credential"
    COMPRESS = "compress"
    ABORT = "abort"


class ClassifiedError(BaseModel):
    """Structured classification of an API error with recovery hints."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    reason: FailoverReason
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    recovery: RecoveryAction = RecoveryAction.RETRY_AFTER_BACKOFF

    status_code: int | None = None
    provider: str | None = None
    model: str | None = None
    message: str = ""
    error_context: dict[str, Any] = Field(default_factory=dict)

    @property
    def is_auth(self) -> bool:
        return self.reason in (FailoverReason.AUTH, FailoverReason.AUTH_PERMANENT)

    @property
    def user_facing_summary(self) -> str:
        """One-line, non-technical summary suitable for chat / toast display."""
        if self.reason == FailoverReason.RATE_LIMIT:
            return "Provider rate limit — retrying with backoff."
        if self.reason == FailoverReason.OVERLOADED:
            return "Provider overloaded — retrying."
        if self.reason == FailoverReason.SERVER_ERROR:
            return "Upstream server error — retrying."
        if self.reason == FailoverReason.TIMEOUT:
            return "Connection timeout — retrying."
        if self.reason == FailoverReason.BILLING:
            return "Provider billing problem — no retry."
        if self.reason in (FailoverReason.AUTH, FailoverReason.AUTH_PERMANENT):
            return "Authentication failed — check your API key."
        if self.reason == FailoverReason.CONTEXT_OVERFLOW:
            return "Context too large — consider a shorter prompt or a bigger model."
        if self.reason == FailoverReason.PAYLOAD_TOO_LARGE:
            return "Request too large — trimming / retrying."
        if self.reason == FailoverReason.MODEL_NOT_FOUND:
            return "Model not found or unavailable."
        if self.reason == FailoverReason.FORMAT_ERROR:
            return "Invalid request format."
        return self.message or "Unknown provider error."
