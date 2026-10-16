"""Application texts (English only).

One YAML file per component lives in this package directory. Look up a key by
its dotted path and interpolate ``%{variable}`` placeholders::

    from ..texts import t
    t("booster.react.add.success", emoji=emoji, user=user.mention, extra=extra)
"""

from __future__ import annotations

import random
import re
from functools import lru_cache
from pathlib import Path

import yaml

TEXTS_DIR = Path(__file__).resolve().parent

_INTERPOLATION = re.compile(r"%\{(\w+)\}")


class TextError(Exception):
    """Base class for text lookup problems."""


class MissingText(TextError, KeyError):
    """Raised when a requested key does not exist."""


class MissingInterpolation(TextError, KeyError):
    """Raised when a text uses a ``%{variable}`` that was not provided."""


def _flatten(tree: dict, prefix: tuple[str, ...] = ()):  # noqa: ANN202
    for key, value in tree.items():
        if isinstance(value, dict):
            yield from _flatten(value, prefix + (key,))
        else:
            yield ".".join(prefix + (key,)), value


@lru_cache(maxsize=1)
def _load_texts() -> dict[str, str]:
    texts: dict[str, str] = {}
    for path in sorted(TEXTS_DIR.glob("*.yml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        texts.update(dict(_flatten(data)))
    return texts


def _interpolate(text: str, variables: dict) -> str:
    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables:
            raise MissingInterpolation(
                f"Missing '%{{{name}}}' for text: {text!r}"
            )
        return str(variables[name])

    return _INTERPOLATION.sub(replace, text)


def t(key: str, **variables) -> str:
    """Return the text for ``key`` with ``variables`` filled in."""
    try:
        text = _load_texts()[key]
    except KeyError as exc:
        raise MissingText(key) from exc

    if not isinstance(text, str):
        raise MissingText(f"{key} is not a string")

    return _interpolate(text, variables) if variables else text


def t_choice(key: str, **variables) -> str:
    """Pick one entry at random from a list of texts, then interpolate it.

    A key holding a single string is accepted and returned as-is.
    """
    try:
        value = _load_texts()[key]
    except KeyError as exc:
        raise MissingText(key) from exc

    if isinstance(value, str):
        entries = [value]
    elif isinstance(value, list) and value:
        entries = value
    else:
        raise MissingText(f"{key} is not a non-empty list of strings")

    text = random.choice(entries)
    if not isinstance(text, str):
        raise MissingText(f"{key} is not a list of strings")

    return _interpolate(text, variables) if variables else text
