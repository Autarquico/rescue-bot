"""Redacción básica de secretos antes de que una salida llegue al modelo. No es completa."""

from __future__ import annotations

import re

MASK = "[REDACTED]"

_PATTERNS = [
    re.compile(
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)", re.S
    ),
    re.compile(r"(?i)\b(authorization\s*[:=]\s*)(?:(?:bearer|basic|token)\s+)?[^\s\"']+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{8,}"),
    re.compile(
        r"(?i)([\w.-]*(?:password|passwd|secret|token|api[_-]?key)[\w.-]*[\"']?\s*[=:]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|\S+)"
    ),
    re.compile(r"(\b[A-Za-z][\w+.-]*://[^:/\s@]+:)[^@\s/]+(@)"),
    re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bxapp-[A-Za-z0-9-]{8,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b"),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}"),
]


def _mask(m: re.Match) -> str:
    """Conserva el prefijo (grupo 1) y, si existe, el sufijo (grupo 2)."""
    suffix = m.group(2) if m.re.groups > 1 else ""
    return m.group(1) + MASK + suffix


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(_mask if pattern.groups else MASK, text)
    return text
