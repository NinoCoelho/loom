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


class TestRetry:
    def test_backoff_increases(self):
        delays = [jittered_backoff(i) for i in range(5)]
        for i in range(1, len(delays)):
            assert delays[i] >= delays[i - 1] * 0.3

    def test_backoff_capped(self):
        assert jittered_backoff(100) <= 60.0
