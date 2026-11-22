"""Hour-of-day validation shared by the features that schedule by hour."""

from __future__ import annotations

from typing import TypeVar

MIN_HOUR = 0
MAX_HOUR = 23

E = TypeVar("E", bound=Exception)


def check_hour(hour: int, error: type[E]) -> int:
    """Return ``hour`` if it is a whole hour of the day, else raise ``error(hour=hour)``."""
    if not isinstance(hour, int) or not MIN_HOUR <= hour <= MAX_HOUR:
        raise error(hour=hour)
    return hour
