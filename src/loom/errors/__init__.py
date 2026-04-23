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
"""

from loom.errors._classify import classify_api_error, classify_http
from loom.errors._types import (
    ClassifiedError,
    FailoverReason,
    LLMError,
    LLMTransportError,
    MalformedOutputError,
    RecoveryAction,
)

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
