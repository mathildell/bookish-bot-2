"""Text fitting and wrapping helpers shared by generated images."""

from __future__ import annotations

from pathlib import Path

from PIL import ImageDraw, ImageFont

ELLIPSIS = "…"


def fit_single_line(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    start_size: int,
    max_width: int,
    *,
    min_size: int = 10,
) -> tuple[ImageFont.FreeTypeFont, str]:
    """Largest font (down to ``min_size``) and text that fit ``max_width``."""
    size = start_size
    while size > min_size:
        font = ImageFont.truetype(str(font_path), size)
        if draw.textlength(text, font=font) <= max_width:
            return font, text
        size -= 1
    font = ImageFont.truetype(str(font_path), min_size)
    truncated = text
    while truncated and draw.textlength(
        f"{truncated}{ELLIPSIS}", font=font
    ) > max_width:
        truncated = truncated[:-1]
    return font, f"{truncated}{ELLIPSIS}"


def wrap_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.FreeTypeFont,
    max_width: int,
) -> list[str]:
    """Wrap text to ``max_width``, preserving explicit line breaks."""
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        line = ""
        for word in words:
            candidate = f"{line} {word}".strip()
            if draw.textlength(candidate, font=font) <= max_width:
                line = candidate
                continue
            if line:
                lines.append(line)
                line = ""
            if draw.textlength(word, font=font) <= max_width:
                line = word
                continue
            chunk = ""
            for character in word:
                if draw.textlength(f"{chunk}{character}", font=font) <= max_width:
                    chunk = f"{chunk}{character}"
                else:
                    if chunk:
                        lines.append(chunk)
                    chunk = character
            line = chunk
        if line:
            lines.append(line)
    return lines or [""]


def line_height(font: ImageFont.FreeTypeFont, spacing: float = 1.12) -> int:
    """The vertical distance between two wrapped lines."""
    return max(1, int(font.size * spacing))


def fit_wrapped(
    draw: ImageDraw.ImageDraw,
    text: str,
    font_path: Path,
    start_size: int,
    max_width: int,
    max_height: int,
    *,
    min_size: int = 12,
    spacing: float = 1.12,
) -> tuple[ImageFont.FreeTypeFont, list[str]]:
    """Largest wrapped text (down to ``min_size``) fitting the given box."""
    size = start_size
    while size >= min_size:
        font = ImageFont.truetype(str(font_path), size)
        lines = wrap_text(draw, text, font, max_width)
        height = line_height(font, spacing) * len(lines)
        if height <= max_height:
            return font, lines
        size -= 1

    font = ImageFont.truetype(str(font_path), min_size)
    lines = wrap_text(draw, text, font, max_width)
    height = line_height(font, spacing)
    max_lines = max(1, max_height // height)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(
            f"{last}{ELLIPSIS}", font=font
        ) > max_width:
            last = last[:-1]
        lines[-1] = f"{last}{ELLIPSIS}"
    return font, lines
