"""Render bi-weekly theme cards onto the shared template."""

from __future__ import annotations

import os
import tempfile
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from .image_text import fit_single_line, fit_wrapped, line_height

_ASSETS = Path(__file__).resolve().parents[2] / "assets"
TEMPLATE = _ASSETS / "weeklytheme" / "template.png"
FONT_REGULAR = _ASSETS / "fonts" / "FlowerBee.ttf"

THEME_IMGBB_EXPIRATION_SECONDS = 90 * 24 * 3600

DATE_FROM_CENTER = (635, 190)
DATE_FROM_MAX_WIDTH = 280
DATE_TO_CENTER = (1095, 190)
DATE_TO_MAX_WIDTH = 230
DATE_SIZE = 48
DATE_MIN_SIZE = 20

PROMPT_LEFT = 395
PROMPT_TOP = 240
PROMPT_WIDTH = 820
PROMPT_HEIGHT = 480
PROMPT_TITLE_SIZE = 55
PROMPT_DESCRIPTION_SIZE = 34
PROMPT_GAP = 10
PROMPT_SPACING = 1.12


class ThemeRenderError(Exception):
    """Raised when the theme template or font cannot be rendered."""

def format_theme_date(day: date) -> str:
    """Render a theme date the way it appears on the card."""
    return f"{day.strftime('%b')} {day.day}, {day.year}"

def theme_period(posted_on: date) -> tuple[str, str]:
    """The inclusive period a theme covers: the next 14 days after posting."""
    start = posted_on + timedelta(days=1)
    end = start + timedelta(days=13)
    return format_theme_date(start), format_theme_date(end)

def _draw_lines(
    draw: ImageDraw.ImageDraw,
    lines: list[str],
    font: ImageFont.FreeTypeFont,
    *,
    x: int,
    y: int,
    spacing: float,
) -> int:
    height = line_height(font, spacing)
    for index, line in enumerate(lines):
        draw.text(
            (x, y + index * height),
            line,
            font=font,
            fill=(255, 255, 255),
            anchor="la",
        )
    return height * len(lines)


def render_theme(
    *,
    prompt: str,
    description: str | None,
    start: str,
    end: str,
) -> Path:
    """Draw the theme card and return the temporary PNG path."""
    try:
        with Image.open(TEMPLATE) as opened:
            image = opened.convert("RGBA")
        draw = ImageDraw.Draw(image)

        for text, center, max_width in (
            (start, DATE_FROM_CENTER, DATE_FROM_MAX_WIDTH),
            (end, DATE_TO_CENTER, DATE_TO_MAX_WIDTH),
        ):
            font, fitted = fit_single_line(
                draw,
                text,
                FONT_REGULAR,
                DATE_SIZE,
                max_width,
                min_size=DATE_MIN_SIZE,
            )
            draw.text(
                center,
                fitted,
                font=font,
                fill=(255, 255, 255),
                anchor="ms",
            )

        title_font, title_lines = fit_wrapped(
            draw,
            prompt,
            FONT_REGULAR,
            PROMPT_TITLE_SIZE,
            PROMPT_WIDTH,
            int(PROMPT_HEIGHT * 0.55),
            spacing=PROMPT_SPACING,
        )
        blocks: list[
            tuple[ImageFont.FreeTypeFont, list[str]]
        ] = [(title_font, title_lines)]
        title_height = line_height(title_font, PROMPT_SPACING) * len(title_lines)

        if description:
            remaining = PROMPT_HEIGHT - title_height - PROMPT_GAP
            description_font, description_lines = fit_wrapped(
                draw,
                description,
                FONT_REGULAR,
                PROMPT_DESCRIPTION_SIZE,
                PROMPT_WIDTH,
                max(1, remaining),
                spacing=PROMPT_SPACING,
            )
            blocks.append((description_font, description_lines))

        total_height = sum(
            line_height(font, PROMPT_SPACING) * len(lines)
            for font, lines in blocks
        ) + (PROMPT_GAP if len(blocks) > 1 else 0)
        y = PROMPT_TOP + max(0, (PROMPT_HEIGHT - total_height) // 2)

        for index, (font, lines) in enumerate(blocks):
            if index:
                y += PROMPT_GAP
            height = _draw_lines(
                draw,
                lines,
                font,
                x=PROMPT_LEFT,
                y=y,
                spacing=PROMPT_SPACING,
            )
            y += height
    except Exception as exc:
        raise ThemeRenderError(str(exc)) from exc

    handle, path = tempfile.mkstemp(prefix="card-", suffix=".png")
    os.close(handle)
    image.save(path, "PNG")
    return Path(path)
