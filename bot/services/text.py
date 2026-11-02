"""Shared text helpers."""

from __future__ import annotations


def truncate(value: str, limit: int) -> str:
    """Shorten ``value`` to at most ``limit`` characters, marking the cut."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"
