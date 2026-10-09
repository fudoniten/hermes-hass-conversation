"""Pick the acknowledgment spoken when Hermes is slow."""

from __future__ import annotations

import re

from .const import ACK_PATTERNS

_COMPILED = [(re.compile(pattern, re.IGNORECASE), ack) for pattern, ack in ACK_PATTERNS]


def select_ack(text: str, rich_acks: bool, fallback: str) -> str:
    """Return the first matching templated ack, or the fallback."""
    if rich_acks:
        for pattern, ack in _COMPILED:
            if pattern.search(text):
                return ack
    return fallback
