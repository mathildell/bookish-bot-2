"""Shared search helpers for punctuation- and spacing-insensitive matching."""

from __future__ import annotations

import re

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def search_key(value: str) -> str:
    """Fold ``value`` for punctuation- and spacing-insensitive matching.

    Only letters and digits survive (lower-cased), so ``"KJ Charles"`` and
    ``"K. J. Charles"`` both fold to ``"kjcharles"``.
    """
    return "".join(_WORD_RE.findall((value or "").lower()))


def search_key_contains(haystack: str, needle_key: str) -> bool:
    """Whether ``needle_key`` is contained in the folded ``haystack``."""
    if not needle_key:
        return False
    return needle_key in search_key(haystack)
