"""Redaction of regulated / sensitive data before it is written to logs,
transcripts or artifacts.

Two inputs:

* **known secret values** -- the resolved values of input params flagged
  ``sensitive`` in the capability schema, plus any caller-supplied secrets;
  masked by exact substring.
* **pattern-based PII** -- emails, phone numbers, SSNs, long digit runs, and
  dates of birth; masked by regex as a backstop for values we did not know to
  flag.

The live observation shown to the LLM *during* discovery is not redacted (the
agent must read member names etc. to verify state); redaction is applied only to
the copy that is persisted. This module is the single chokepoint -- Phase 6/7
route every write through :func:`redact`.
"""

from __future__ import annotations

import re

MASK = "[REDACTED]"

# Digits often abut label text in this app's markup ("Phone555-0231"), so the
# numeric patterns use digit lookarounds rather than \b.
_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("ssn", re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    ("phone_full", re.compile(
        r"(?<!\d)(?:\+?1[-.\s]?)?(?:\(\d{3}\)|\d{3})[-.\s]?\d{3}[-.\s]?\d{4}(?!\d)")),
    ("phone_local", re.compile(r"(?<!\d)\d{3}-\d{4}(?!\d)")),
    # (?!T\d{2}:) excludes the date portion of an ISO timestamp ("...T12:41:25Z")
    # -- those aren't DOBs, and every evidence file has several.
    ("dob", re.compile(r"(?<!\d)(?:19|20)\d{2}-\d{2}-\d{2}(?!\d)(?!T\d{2}:)")),
    ("card", re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")),
]


def redact(text: str, secrets: list[str] | None = None) -> str:
    """Return ``text`` with known secrets and PII patterns masked."""
    if not text:
        return text
    out = text
    for s in sorted(filter(None, secrets or []), key=len, reverse=True):
        out = out.replace(s, MASK)
    for _name, rx in _PATTERNS:
        out = rx.sub(MASK, out)
    return out


def redact_mapping(data: dict, secrets: list[str] | None = None) -> dict:
    """Deep-copy ``data`` with all string leaves redacted."""
    def _walk(v):
        if isinstance(v, str):
            return redact(v, secrets)
        if isinstance(v, dict):
            return {k: _walk(x) for k, x in v.items()}
        if isinstance(v, list):
            return [_walk(x) for x in v]
        return v

    return _walk(data)


def mask_value(value: object) -> str:
    """Mask a value entirely except a short shape hint (for sensitive outputs
    that must still be debuggable: '4210.55' -> 'nn.nn')."""
    s = str(value)
    return re.sub(r"\d", "n", re.sub(r"[A-Za-z]", "x", s))
