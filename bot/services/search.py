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
