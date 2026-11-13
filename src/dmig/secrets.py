"""Keep credentials out of everything the skill writes.

Logs capture full command output, and restore output can echo feed URLs or configuration; patches
could carry a token if a repair ever touched NuGet.config. Logs are redacted as they are written;
patches are scanned and refused if they contain anything that looks like a credential.
"""
from __future__ import annotations

import re

# (name, pattern with the secret value in group "v")
PATTERNS = [
    ("connection-string password", re.compile(r"(?i)\b(?:password|pwd)\s*=\s*(?P<v>[^;\"'\s<>]{3,})")),
    ("nuget cleartext password", re.compile(r"(?i)ClearTextPassword\"?\s*(?:value=|=|:)\s*\"?(?P<v>[^\"'\s<>]{3,})")),
    ("authorization header", re.compile(r"(?i)\bAuthorization:\s*(?:Bearer|Basic)\s+(?P<v>[A-Za-z0-9._~+/=\-]{8,})")),
    ("token in url", re.compile(r"(?i)https?://[^/\s:@]+:(?P<v>[^@\s/]{6,})@")),
    ("api key / token assignment", re.compile(
        r"(?i)\b(?:api[_-]?key|access[_-]?token|auth[_-]?token|secret|personal[_-]?access[_-]?token)\b\s*[=:]\s*"
        r"[\"']?(?P<v>[A-Za-z0-9._~+/\-]{12,})")),
    ("github token", re.compile(r"(?P<v>\bgh[pousr]_[A-Za-z0-9]{30,})")),
    ("azure devops / gitlab token", re.compile(r"(?P<v>\bglpat-[A-Za-z0-9_\-]{16,})")),
]
REDACTED = "***REDACTED***"


def redact(text: str) -> str:
    """Replace every secret value with ***REDACTED***, keeping the surrounding text readable."""
    for _, pat in PATTERNS:
        text = pat.sub(lambda m: m.group(0).replace(m.group("v"), REDACTED), text)
    return text


def find_secrets(text: str) -> list[str]:
    """Names of the credential patterns present in text (values are never returned)."""
    return sorted({name for name, pat in PATTERNS if any(m.group("v") != REDACTED for m in pat.finditer(text))})


def added_lines(patch: str) -> str:
    """Only the lines a patch adds: secrets already in the repository are not the patch's doing."""
    return "\n".join(l[1:] for l in patch.splitlines() if l.startswith("+") and not l.startswith("+++"))
