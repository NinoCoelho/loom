"""Log redaction — replace secrets with safe placeholder tokens.

:func:`redact_sensitive_text` applies a cascade of regex patterns to scrub API keys,
tokens, connection strings, private keys, and other sensitive values from text
before it is written to logs. Already-redacted spans are preserved so
re-entrant calls do not double-redact.
"""

from __future__ import annotations

import re

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("api_key", re.compile(r"sk-[a-zA-Z0-9]{20,}")),
    ("anthropic_key", re.compile(r"sk-ant-[a-zA-Z0-9\-_]{20,}")),
    ("github_pat", re.compile(r"ghp_[a-zA-Z0-9]{36}")),
    ("github_oauth", re.compile(r"gho_[a-zA-Z0-9]{36}")),
    ("github_app", re.compile(r"ghs_[a-zA-Z0-9]{36}")),
    ("github_refresh", re.compile(r"ghr_[a-zA-Z0-9]{36}")),
    ("slack_bot", re.compile(r"xoxb-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}")),
    ("slack_user", re.compile(r"xoxp-[0-9]{10,13}-[0-9]{10,13}-[0-9]{10,13}-[a-zA-Z0-9]{24}")),
    ("slack_app", re.compile(r"xapp-[0-9]{10,13}-[a-zA-Z0-9]{24,}")),
    ("google_api", re.compile(r"AIza[a-zA-Z0-9\-_]{35}")),
    ("aws_access_key", re.compile(r"AKIA[A-Z0-9]{16}")),
    (
        "aws_secret_key",
        re.compile(
            r"(?:AWSSecretKey=|aws_secret_access_key=|AWS_SECRET_ACCESS_KEY=)[a-zA-Z0-9/+=]{40}"
        ),
    ),
    ("stripe_live", re.compile(r"[sr]k_live_[a-zA-Z0-9]{24,}")),
    ("stripe_test", re.compile(r"[sr]k_test_[a-zA-Z0-9]{24,}")),
    ("sendgrid", re.compile(r"SG\.[a-zA-Z0-9\-_]{22,}\.[a-zA-Z0-9\-_]{43,}")),
    ("twilio", re.compile(r"SK[a-fA-F0-9]{32}")),
    ("mailgun", re.compile(r"key-[a-zA-Z0-9]{32}")),
    ("digitalocean", re.compile(r"dop_v1_[a-f0-9]{64}")),
    ("heroku", re.compile(r"heroku_[a-zA-Z0-9\-_]{20,}")),
    ("firebase", re.compile(r"AIzaSy[a-zA-Z0-9\-_]{33}")),
    (
        "azure_tenant",
        re.compile(
            r"(?i)azure[_\-]?tenant[_\-]?id[\"'\s:=]+[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}"
        ),
    ),
    (
        "azure_secret",
        re.compile(r"(?i)azure[_\-]?client[_\-]?secret[\"'\s:=]+[a-zA-Z0-9\-_\.]{20,}"),
    ),
    ("jwt", re.compile(r"eyJ[a-zA-Z0-9\-_]+\.eyJ[a-zA-Z0-9\-_]+\.[a-zA-Z0-9\-_]+")),
    ("bearer_token", re.compile(r"[Bb]earer [a-zA-Z0-9\-_\.]{20,}")),
    ("basic_auth", re.compile(r"[Bb]asic [a-zA-Z0-9\+/=]{20,}")),
    (
        "pem_private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY[^-]*-----"
            r"[\s\S]*?-----END (?:RSA |EC |DSA |OPENSSH )?PRIVATE KEY[^-]*-----"
        ),
    ),
    (
        "db_connection_string",
        re.compile(r"(?:postgres(?:ql)?|mysql|mongodb|redis|mssql|amqp)://[^\s\"']+"),
    ),
    ("phone_number", re.compile(r"(?:\+?\d{1,3}[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")),
]

_JSON_FIELD_PATTERN = re.compile(
    r"""(?x)
    (?:
        " (?:
            api_?key | secret_?key | secret | token | password | passwd |
            credential | auth_?token | access_?token | private_?key
        )
        " \s* : \s* " [^"]{8,} "
    )
"""
)

_ENV_VAR_PATTERN = re.compile(
    r"""(?x)
    (?:
        (?:^|[\s"'`;|&()])
        ([A-Z][A-Z0-9_]{2,}
        (?:KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|AUTH|ACCESS_KEY|PRIVATE_KEY))
        =
        (["']? [a-zA-Z0-9!@#$%^&*()\-_=+\[\]{}|;:,.<>?/~`] {12,} ["']?)
    )
"""
)

_REDACTED_ESCAPE = re.compile(r"\[REDACTED_[a-z_]+\]")


def redact_sensitive_text(text: str) -> str:
    already_redacted = set(str(m) for m in _REDACTED_ESCAPE.finditer(text))

    for label, pattern in _PATTERNS:

        def _replace(m: re.Match[str], _label: str = label) -> str:
            matched = m.group(0)
            if matched in already_redacted:
                return matched
            return f"[REDACTED_{_label}]"

        text = pattern.sub(_replace, text)

    def _replace_json_field(m: re.Match[str]) -> str:
        matched = m.group(0)
        if matched in already_redacted:
            return matched
        key_part = matched.split(":")[0].strip().strip('"').lower()
        key_label = key_part.replace(" ", "_")
        val_start = matched.index(":") + 1
        prefix = matched[:val_start]
        return f'{prefix} "[REDACTED_{key_label}]"'

    text = _JSON_FIELD_PATTERN.sub(_replace_json_field, text)

    def _replace_env(m: re.Match[str]) -> str:
        matched = m.group(0)
        if matched in already_redacted:
            return matched
        key = m.group(1) if m.group(1) else m.group(0).split("=")[0]
        return f"{key}=[REDACTED_env_var]"

    text = _ENV_VAR_PATTERN.sub(_replace_env, text)

    return text
