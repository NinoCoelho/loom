"""Tests for log redaction — :func:`loom.llm.redact.redact_sensitive_text`."""

from loom.llm.redact import redact_sensitive_text


class TestRedaction:
    def test_openai_key(self):
        text = "key is sk-abc123def456ghi789jkl012mno345pqr678"
        assert "sk-" not in redact_sensitive_text(text)
        assert "REDACTED" in redact_sensitive_text(text)

    def test_bearer_token(self):
        text = "Authorization: Bearer abc123def456ghi789jkl012"
        assert "Bearer abc" not in redact_sensitive_text(text)

    def test_jwt(self):
        text = "token: eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.abc123"
        redacted = redact_sensitive_text(text)
        assert "REDACTED" in redacted

    def test_idempotent(self):
        text = "already [REDACTED_api_key] here"
        assert redact_sensitive_text(text) == text

    def test_clean_text(self):
        text = "Hello world, no secrets here"
        assert redact_sensitive_text(text) == text
