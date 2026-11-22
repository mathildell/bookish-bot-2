"""Shared text helpers."""

from __future__ import annotations

from ..texts import t


def truncate(value: str, limit: int) -> str:
    """Shorten ``value`` to at most ``limit`` characters, marking the cut."""
    if len(value) <= limit:
        return value
    return value[: limit - 1].rstrip() + "…"


def ordinal(number: int) -> str:
    """``16`` -> ``"16th"``, honouring the 11th/12th/13th exception."""
    remainder = number % 100
    if 11 <= remainder <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(number % 10, "th")
    return t(
        "components.ordinal.format",
        number=number,
        suffix=t(f"components.ordinal.{suffix}"),
    )
