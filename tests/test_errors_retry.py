from loom.errors import (
    ClassifiedError,
    FailoverReason,
    LLMError,
    LLMTransportError,
    MalformedOutputError,
    RecoveryAction,
    classify_api_error,
    classify_http,
)
from loom.retry import jittered_backoff


class TestErrors:
    def test_transport_error(self):
        e = LLMTransportError("fail", status_code=429, body="rate limit")
        assert e.status_code == 429
        assert "rate limit" in e.body

    def test_malformed_output(self):
        e = MalformedOutputError("bad json")
        assert isinstance(e, LLMError)

    def test_classify_http_429(self):
        c = classify_http(429, "rate limit")
        assert c.reason == FailoverReason.RATE_LIMIT
        assert c.retryable
        assert c.recovery == RecoveryAction.RETRY_AFTER_BACKOFF

    def test_classify_http_401(self):
        c = classify_http(401, "")
        assert c.reason == FailoverReason.AUTH
        assert c.should_rotate_credential

    def test_classify_http_500(self):
        c = classify_http(500, "")
        assert c.reason == FailoverReason.SERVER_ERROR
        assert c.retryable

    def test_classify_http_404(self):
        c = classify_http(404, "")
        assert c.reason == FailoverReason.MODEL_NOT_FOUND
        assert not c.retryable

    def test_classify_http_body_pattern(self):
        c = classify_http(200, "context length exceeded")
        assert c.reason == FailoverReason.CONTEXT_OVERFLOW
        assert c.should_compress

    def test_classify_malformed(self):
        c = classify_api_error(MalformedOutputError("bad"))
        assert c.reason == FailoverReason.FORMAT_ERROR
        assert not c.retryable

    def test_classify_transport(self):
        c = classify_api_error(LLMTransportError("fail", status_code=503))
        assert c.retryable

    def test_classify_unknown(self):
        c = classify_api_error(ValueError("weird"))
        assert c.reason == FailoverReason.UNKNOWN


class TestClassifierAdvanced:
    def test_402_transient_usage_limit_becomes_rate_limit(self):
        c = classify_http(402, "usage limit exceeded, try again in 60s")
        assert c.reason == FailoverReason.RATE_LIMIT
        assert c.retryable
        assert c.should_rotate_credential

    def test_402_billing_exhaustion_aborts(self):
        c = classify_http(402, "credit balance has been exhausted")
        assert c.reason == FailoverReason.BILLING
        assert not c.retryable
        assert c.recovery == RecoveryAction.ABORT

    def test_403_spending_limit_routes_to_billing(self):
        c = classify_http(403, "key limit exceeded")
        assert c.reason == FailoverReason.BILLING
        assert c.should_rotate_credential

    def test_400_context_overflow_from_body_pattern(self):
        c = classify_http(400, "prompt is too long, reduce the length")
        assert c.reason == FailoverReason.CONTEXT_OVERFLOW
        assert c.should_compress

    def test_429_long_context_tier_anthropic(self):
        err = LLMTransportError(
            "extra usage on the long context tier",
            status_code=429,
            body='{"error":{"message":"extra usage on the long context tier"}}',
        )
        c = classify_api_error(err)
        assert c.reason == FailoverReason.LONG_CONTEXT_TIER
        assert c.should_compress

    def test_400_thinking_signature_anthropic(self):
        err = LLMTransportError(
            "invalid signature for thinking block",
            status_code=400,
            body="",
        )
        c = classify_api_error(err)
        assert c.reason == FailoverReason.THINKING_SIGNATURE
        assert c.retryable

    def test_400_generic_body_with_large_session_becomes_context_overflow(self):
        err = LLMTransportError(
            "error",
            status_code=400,
            body='{"error":{"message":"error"}}',
        )
        c = classify_api_error(err, approx_tokens=100_000, num_messages=120)
        assert c.reason == FailoverReason.CONTEXT_OVERFLOW
        assert c.should_compress

    def test_400_generic_body_small_session_becomes_format_error(self):
        err = LLMTransportError(
            "error",
            status_code=400,
            body='{"error":{"message":"error"}}',
        )
        c = classify_api_error(err, approx_tokens=500, num_messages=3)
        assert c.reason == FailoverReason.FORMAT_ERROR
        assert not c.retryable

    def test_server_disconnect_with_huge_context_becomes_overflow(self):
        err = LLMTransportError("server disconnected without response")
        c = classify_api_error(err, approx_tokens=150_000)
        assert c.reason == FailoverReason.CONTEXT_OVERFLOW

    def test_server_disconnect_small_becomes_timeout(self):
        err = LLMTransportError("server disconnected without response")
        c = classify_api_error(err, approx_tokens=500)
        assert c.reason == FailoverReason.TIMEOUT

    def test_message_based_billing_no_status(self):
        err = LLMTransportError("insufficient credits on your account")
        c = classify_api_error(err)
        assert c.reason == FailoverReason.BILLING
        assert not c.retryable

    def test_user_facing_summary_is_non_technical(self):
        c = classify_http(429, "")
        assert "rate limit" in c.user_facing_summary.lower()

    def test_context_propagates_to_classified_error(self):
        err = LLMTransportError("boom", status_code=500)
        c = classify_api_error(err, provider="anthropic", model="claude-sonnet-4-6")
        assert c.provider == "anthropic"
        assert c.model == "claude-sonnet-4-6"
        assert c.status_code == 500


class TestRetry:
    def test_backoff_increases(self):
        delays = [jittered_backoff(i) for i in range(5)]
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1] * 0.3

    def test_backoff_capped(self):
        assert jittered_backoff(100) <= 60.0
