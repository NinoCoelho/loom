from __future__ import annotations

import re
from loom.skills.types import SkillGuardVerdict


_DANGEROUS_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"(?:curl|wget)\b.*\$(?:API_KEY|TOKEN|SECRET|PASSWORD|ACCESS_KEY|PRIVATE_KEY)", re.IGNORECASE),
        "Possible credential exfiltration: network request with environment variable",
    ),
    (
        re.compile(r"cat\b.*~/(?:\.ssh|\.aws|\.gcloud)", re.IGNORECASE),
        "Possible credential exfiltration: reading sensitive credential directory",
    ),
    (
        re.compile(r"base64\b.*\$(?:API_KEY|TOKEN|SECRET|PASSWORD|ACCESS_KEY|PRIVATE_KEY)", re.IGNORECASE),
        "Possible credential exfiltration: base64 encoding of environment variable",
    ),
    (
        re.compile(r"rm\s+-rf\s+/(?:\s|$)", re.IGNORECASE),
        "Destructive command: rm -rf /",
    ),
    (
        re.compile(r"\bdd\s+if=", re.IGNORECASE),
        "Destructive command: dd disk write",
    ),
    (
        re.compile(r"\bmkfs\b", re.IGNORECASE),
        "Destructive command: mkfs filesystem format",
    ),
    (
        re.compile(r":\(\)\{\s*:\|:&\s*\}", re.IGNORECASE),
        "Destructive command: fork bomb",
    ),
    (
        re.compile(r"ignore\s+previous\s+instructions?", re.IGNORECASE),
        "Prompt injection attempt: 'ignore previous instructions'",
    ),
    (
        re.compile(r"disregard\s+(?:your|all|the|my)\s+instructions?", re.IGNORECASE),
        "Prompt injection attempt: 'disregard your instructions'",
    ),
    (
        re.compile(r"forget\s+(?:your|all|the|my)\s+instructions?", re.IGNORECASE),
        "Prompt injection attempt: 'forget your instructions'",
    ),
]

_CAUTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (
        re.compile(r"\bcron(?:tab)?\b", re.IGNORECASE),
        "Persistence mechanism: cron/crontab",
    ),
    (
        re.compile(r"\blaunchd\b", re.IGNORECASE),
        "Persistence mechanism: launchd",
    ),
    (
        re.compile(r"\bsystemd\b", re.IGNORECASE),
        "Persistence mechanism: systemd",
    ),
    (
        re.compile(r"\.(?:bashrc|profile|zshrc)\b", re.IGNORECASE),
        "Persistence mechanism: shell profile modification",
    ),
]


class SkillGuard:
    def scan(self, content: str, filename: str = "") -> SkillGuardVerdict:
        findings: list[str] = []
        level = "safe"

        for pattern, description in _DANGEROUS_PATTERNS:
            if pattern.search(content):
                findings.append(description)
                level = "dangerous"

        for pattern, description in _CAUTION_PATTERNS:
            if pattern.search(content):
                findings.append(description)
                if level == "safe":
                    level = "caution"

        return SkillGuardVerdict(level=level, findings=findings)
