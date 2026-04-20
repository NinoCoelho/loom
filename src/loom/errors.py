"""API error classification for smart failover and recovery.

Rich, priority-ordered classification pipeline for LLM provider errors.
The agent loop's retry logic consumes the resulting :class:`ClassifiedError`
rather than pattern-matching exceptions inline — this keeps recovery policy
centralised and provider-agnostic.

Pipeline (in order):

1. Provider-specific patterns (Anthropic thinking signatures, long-context tier)
2. HTTP status code + message-aware refinement
3. Error code classification (from structured body)
4. Message pattern matching (billing vs rate_limit vs context vs auth)
5. Transport / timeout heuristics
6. Server disconnect + large session → context overflow
7. Fallback: UNKNOWN (retryable with backoff)

The heuristics in (6) and parts of (2) take optional context kwargs
(``approx_tokens``, ``context_length``, ``num_messages``) so callers can give
the classifier enough signal to disambiguate generic 400s / peer disconnects.
"""

from __future__ import annotations

import json
import re
from enum import Enum
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

# ── Exceptions ──────────────────────────────────────────────────────────


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


# ── Taxonomy ────────────────────────────────────────────────────────────


class FailoverReason(str, Enum):
    """Why an LLM call failed — drives recovery strategy."""

    # Authentication / authorization
    AUTH = "auth"                              # Transient auth (401/403) — rotate/refresh
    AUTH_PERMANENT = "auth_permanent"          # Auth still fails after refresh — abort

    # Billing / quota
    BILLING = "billing"                        # 402 / confirmed exhaustion — rotate immediately
    RATE_LIMIT = "rate_limit"                  # 429 / transient quota — backoff + rotate

    # Server-side
    OVERLOADED = "overloaded"                  # 503 / 529 — backoff
    SERVER_ERROR = "server_error"              # 500 / 502 — retry

    # Transport
    TIMEOUT = "timeout"                        # Connection / read timeout

    # Context / payload
    CONTEXT_OVERFLOW = "context_overflow"      # Too large for model — compress
    PAYLOAD_TOO_LARGE = "payload_too_large"    # 413

    # Model
    MODEL_NOT_FOUND = "model_not_found"        # 404 / invalid model — fallback

    # Request format
    FORMAT_ERROR = "format_error"              # 400 bad request — abort / strip + retry

    # Legacy reasons kept for back-compat with 0.2 callers.
    BAD_REQUEST = "bad_request"
    NOT_FOUND = "not_found"

    # Provider-specific
    THINKING_SIGNATURE = "thinking_signature"  # Anthropic thinking block sig invalid
    LONG_CONTEXT_TIER = "long_context_tier"    # Anthropic extra-usage tier gate

    # Catch-all
    UNKNOWN = "unknown"


class RecoveryAction(str, Enum):
    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    ROTATE_CREDENTIAL = "rotate_credential"
    COMPRESS = "compress"
    ABORT = "abort"


# ── Result model ────────────────────────────────────────────────────────


class ClassifiedError(BaseModel):
    """Structured classification of an API error with recovery hints."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    reason: FailoverReason
    retryable: bool = True
    should_compress: bool = False
    should_rotate_credential: bool = False
    should_fallback: bool = False
    recovery: RecoveryAction = RecoveryAction.RETRY_AFTER_BACKOFF

    # Rich context (optional — callers can pass these through from the loop)
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
    retryable: bool,
    should_compress: bool,
    should_rotate_credential: bool,
) -> RecoveryAction:
    """Map reason -> primary recovery action. Pure table lookup; the
    ``should_*`` flags are orthogonal hints, not recovery overrides."""
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
    recovery = _derive_recovery(reason, retryable, should_compress, should_rotate_credential)
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


# ── Provider-specific patterns ──────────────────────────────────────────

_BILLING_PATTERNS = [
    "insufficient credits",
    "insufficient_quota",
    "credit balance",
    "credits have been exhausted",
    "top up your credits",
    "payment required",
    "billing hard limit",
    "exceeded your current quota",
    "account is deactivated",
    "plan does not include",
]

_RATE_LIMIT_PATTERNS = [
    "rate limit",
    "rate_limit",
    "too many requests",
    "throttled",
    "requests per minute",
    "tokens per minute",
    "requests per day",
    "try again in",
    "please retry after",
    "resource_exhausted",
    "rate increased too quickly",
    "throttlingexception",
    "too many concurrent requests",
    "servicequotaexceededexception",
]

_USAGE_LIMIT_PATTERNS = [
    "usage limit",
    "quota",
    "limit exceeded",
    "key limit exceeded",
]

_USAGE_LIMIT_TRANSIENT_SIGNALS = [
    "try again",
    "retry",
    "resets at",
    "reset in",
    "wait",
    "requests remaining",
    "periodic",
    "window",
]

_PAYLOAD_TOO_LARGE_PATTERNS = [
    "request entity too large",
    "payload too large",
    "error code: 413",
]

_CONTEXT_OVERFLOW_PATTERNS = [
    "context length",
    "context size",
    "maximum context",
    "token limit",
    "too many tokens",
    "reduce the length",
    "exceeds the limit",
    "context window",
    "prompt is too long",
    "prompt exceeds max length",
    "max_tokens",
    "maximum number of tokens",
    "exceeds the max_model_len",
    "max_model_len",
    "prompt length",
    "input is too long",
    "maximum model length",
    "context length exceeded",
    "truncating input",
    "slot context",
    "n_ctx_slot",
    "超过最大长度",
    "上下文长度",
    "max input token",
    "input token",
    "exceeds the maximum number of input tokens",
]

_MODEL_NOT_FOUND_PATTERNS = [
    "is not a valid model",
    "invalid model",
    "model not found",
    "model_not_found",
    "does not exist",
    "no such model",
    "unknown model",
    "unsupported model",
]

_AUTH_PATTERNS = [
    "invalid api key",
    "invalid_api_key",
    "authentication",
    "unauthorized",
    "forbidden",
    "invalid token",
    "token expired",
    "token revoked",
    "access denied",
]

_TRANSPORT_ERROR_TYPES = frozenset({
    "ReadTimeout", "ConnectTimeout", "PoolTimeout",
    "ConnectError", "RemoteProtocolError",
    "ConnectionError", "ConnectionResetError",
    "ConnectionAbortedError", "BrokenPipeError",
    "TimeoutError", "ReadError",
    "ServerDisconnectedError",
    "APIConnectionError",
    "APITimeoutError",
})

_SERVER_DISCONNECT_PATTERNS = [
    "server disconnected",
    "peer closed connection",
    "connection reset by peer",
    "connection was closed",
    "network connection lost",
    "unexpected eof",
    "incomplete chunked read",
]


# ── Public API ──────────────────────────────────────────────────────────


def classify_http(status: int, body: str = "") -> ClassifiedError:
    """Classify by HTTP status + body message — thin entry point for tests /
    callers that only have an `(status, body)` pair."""
    # Fast-path: look for explicit signals in the body first.
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
    """Classify a raised exception into a recovery recommendation.

    The optional context kwargs let the classifier make context-aware calls for
    ambiguous cases (generic 400 bodies, peer disconnects on huge prompts).
    """
    # Framework-internal exceptions take precedence over HTTP mapping.
    if isinstance(error, MalformedOutputError):
        return _build(
            FailoverReason.FORMAT_ERROR,
            retryable=False,
            provider=provider or None,
            model=model or None,
            message=str(error),
        )

    status_code = _extract_status_code(error)
    error_type = type(error).__name__
    body = _extract_error_body(error)
    error_code = _extract_error_code(body)

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
            message=_extract_message(error, body),
        )
        defaults.update(overrides)
        return _build(reason, **defaults)

    # ── 1. Provider-specific patterns ───────────────────────────────
    if status_code == 400 and "signature" in error_msg and "thinking" in error_msg:
        return _result(FailoverReason.THINKING_SIGNATURE, retryable=True)

    if (
        status_code == 429
        and "extra usage" in error_msg
        and "long context" in error_msg
    ):
        return _result(
            FailoverReason.LONG_CONTEXT_TIER,
            retryable=True,
            should_compress=True,
        )

    # ── 2. HTTP status code classification ──────────────────────────
    if status_code is not None:
        classified = _classify_by_status(
            status_code, error_msg, error_code, body,
            provider=provider_lower, model=model_lower,
            approx_tokens=approx_tokens, context_length=context_length,
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
        error_msg, error_type,
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
            approx_tokens > context_length * 0.6
            or approx_tokens > 120_000
            or num_messages > 200
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

    # Surviving LLMError subclasses with no signal → UNKNOWN (retryable).
    if isinstance(error, LLMError):
        return _result(FailoverReason.UNKNOWN, retryable=True)

    # ── 7. Fallback ─────────────────────────────────────────────────
    return _result(FailoverReason.UNKNOWN, retryable=True)


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
            error_msg, error_code, body,
            provider=provider, model=model,
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


def _classify_402(
    error_msg: str,
    *,
    status: int,
    message: str,
) -> ClassifiedError:
    """Used from classify_http — builds directly."""
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
    """Disambiguate 402: billing exhaustion vs transient usage limit."""
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
    is_large = (
        approx_tokens > context_length * 0.4
        or approx_tokens > 80_000
        or num_messages > 80
    )

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
        has_transient_signal = any(
            p in error_msg for p in _USAGE_LIMIT_TRANSIENT_SIGNALS
        )
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


# ── Helpers ─────────────────────────────────────────────────────────────


def _extract_status_code(error: Exception) -> int | None:
    """Walk the error and its cause chain to find an HTTP status code."""
    current: Any = error
    for _ in range(5):
        code = getattr(current, "status_code", None)
        if isinstance(code, int):
            return code
        code = getattr(current, "status", None)
        if isinstance(code, int) and 100 <= code < 600:
            return code
        cause = getattr(current, "__cause__", None) or getattr(
            current, "__context__", None
        )
        if cause is None or cause is current:
            break
        current = cause
    return None


def _extract_error_body(error: Exception) -> dict:
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        return body
    if isinstance(body, str) and body.strip():
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
    response = getattr(error, "response", None)
    if response is not None:
        try:
            json_body = response.json()
            if isinstance(json_body, dict):
                return json_body
        except Exception:
            pass
    return {}


def _extract_error_code(body: dict) -> str:
    if not body:
        return ""
    error_obj = body.get("error", {})
    if isinstance(error_obj, dict):
        code = error_obj.get("code") or error_obj.get("type") or ""
        if isinstance(code, str) and code.strip():
            return code.strip()
    code = body.get("code") or body.get("error_code") or ""
    if isinstance(code, (str, int)):
        return str(code).strip()
    return ""


def _extract_message(error: Exception, body: dict) -> str:
    if body:
        error_obj = body.get("error", {})
        if isinstance(error_obj, dict):
            msg = error_obj.get("message", "")
            if isinstance(msg, str) and msg.strip():
                return msg.strip()[:500]
        msg = body.get("message", "")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()[:500]
    return str(error)[:500]


__all__ = [
    "ClassifiedError",
    "FailoverReason",
    "LLMError",
    "LLMTransportError",
    "MalformedOutputError",
    "RecoveryAction",
    "classify_api_error",
    "classify_http",
]


# Silence unused-import warnings from tooling that misses the runtime usage.
_ = re
