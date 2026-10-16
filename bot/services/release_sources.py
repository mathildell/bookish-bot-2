"""Read upcoming book data from Goodreads and NetGalley.

Neither site offers an API, so a headless browser (Playwright/Chromium) is used
to render the page and hand back its HTML. Retrieval and parsing are kept apart
on purpose:

- ``parse_book_html`` (and the ``parse_*_html`` helpers) are pure functions that
  turn page HTML into structured data, so they are cheap to unit-test;
- ``ReleaseScraper`` only drives the browser and reuses a single instance.

Errors carry a ``bot/texts`` key so callers can render them with ``t()``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from dataclasses import dataclass
from datetime import date
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from ..texts import t
from .html import html_to_markdown

logger = logging.getLogger("bh2.releases")

GOODREADS_HOST = "goodreads.com"
NETGALLEY_HOSTS = (
    "netgalley.com",
    "netgalley.co.uk",
    "netgalley.fr",
    "netgalley.de",
)

_GOODREADS_PATH_RE = re.compile(r"/book/show/(\d+)")
_NETGALLEY_PATH_RE = re.compile(r"/catalog/book/(\d+)")

_SCRAPE_ATTEMPTS = 3
_RETRYABLE_KEYS = frozenset({"releases.errors.scrape_failed"})

_ISO_DATE_RE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")
_DOTTED_DATE_RE = re.compile(r"\b(\d{1,2})\.(\d{1,2})\.(\d{4})\b")
_MONTH_DAY_RE = re.compile(
    r"\b([A-Za-z\u00C0-\u017F]{3,9})\.?\s+(\d{1,2})(?:,)?\s+(\d{2,4})\b"
)
_DAY_MONTH_RE = re.compile(
    r"\b(\d{1,2})\s+([A-Za-z\u00C0-\u017F]{3,9})\.?\s+(\d{2,4})\b"
)

_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
    "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
    "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
    "janvier": 1, "fevrier": 2, "février": 2, "mars": 3, "avril": 4,
    "mai": 5, "juin": 6, "juillet": 7, "aout": 8, "août": 8,
    "septembre": 9, "octobre": 10, "novembre": 11, "decembre": 12,
    "décembre": 12, "janv": 1, "fevr": 2, "févr": 2, "avr": 4,
    "juil": 7, "déc": 12,
}


class BookScrapeError(Exception):
    """Raised when book data cannot be read. ``key`` is a ``texts`` key."""

    def __init__(self, key: str, **variables) -> None:
        super().__init__(key)
        self.key = key
        self.variables = variables


@dataclass(frozen=True)
class BookData:
    """Normalised data for one upcoming release."""

    title: str
    authors: list[str]
    release_date: date
    blurb: str
    image_url: str | None
    tags: list[str]
    source: str
    source_id: str
    url: str


@dataclass(frozen=True)
class ParsedBook:
    """The fields read from a book page, before the source is attached."""

    title: str
    authors: list[str]
    release_date: date
    blurb: str
    image_url: str | None
    tags: list[str]


def parse_source_url(value: str) -> tuple[str, str]:
    """Return ``(source, source_id)`` for a Goodreads/NetGalley book URL."""
    try:
        parsed = urlparse(value.strip())
    except ValueError as exc:
        raise BookScrapeError("releases.errors.invalid_url") from exc
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in ("http", "https") or not host:
        raise BookScrapeError("releases.errors.invalid_url")

    if host == GOODREADS_HOST or host.endswith("." + GOODREADS_HOST):
        match = _GOODREADS_PATH_RE.search(parsed.path)
        if not match:
            raise BookScrapeError("releases.errors.invalid_path")
        return "goodreads", match.group(1)

    if any(host == h or host.endswith("." + h) for h in NETGALLEY_HOSTS):
        match = _NETGALLEY_PATH_RE.search(parsed.path)
        if not match:
            raise BookScrapeError("releases.errors.invalid_path")
        return "netgalley", match.group(1)

    # NetGalley only renders the dates we parse on these regional sites.
    if "netgalley" in host:
        raise BookScrapeError("releases.errors.unsupported_region")

    raise BookScrapeError("releases.errors.unsupported_source")


def _month_number(name: str) -> int | None:
    return _MONTHS.get(name.lower().strip("."))


def _safe_date(year: str | int, month: int, day: str | int) -> date | None:
    try:
        year_value = int(year)
        # Goodreads shows upcoming dates with a 2-digit year ("27 Oct 26").
        if year_value < 100:
            year_value += 2000
        return date(year_value, int(month), int(day))
    except (TypeError, ValueError):
        return None


def parse_date_text(value: str | None) -> date | None:
    """Extract a release date from free-form text (English or French)."""
    if not value:
        return None

    match = _ISO_DATE_RE.search(value)
    if match:
        return _safe_date(match.group(1), int(match.group(2)), match.group(3))

    match = _MONTH_DAY_RE.search(value)
    if match:
        month = _month_number(match.group(1))
        if month:
            return _safe_date(match.group(3), month, match.group(2))

    match = _DAY_MONTH_RE.search(value)
    if match:
        month = _month_number(match.group(2))
        if month:
            return _safe_date(match.group(3), month, match.group(1))

    match = _DOTTED_DATE_RE.search(value)
    if match:
        return _safe_date(match.group(3), int(match.group(2)), match.group(1))

    return None


_MULTI_NEWLINE_RE = re.compile(r"\n{2,}")


def _condense_newlines(value: str) -> str:
    """Collapse runs of blank lines into a single line break."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return _MULTI_NEWLINE_RE.sub("\n", text).strip()


def _as_text(value) -> str:
    return str(value).strip() if value is not None else ""


def _as_text_list(value) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str) and value.strip():
        return [part.strip() for part in value.split(",") if part.strip()]
    return []


def _author_names(value) -> list[str]:
    items = value if isinstance(value, list) else [value]
    names: list[str] = []
    for item in items:
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, str):
            name = item
        else:
            name = None
        if name and str(name).strip():
            names.append(str(name).strip())
    return names


def _image_url(value) -> str | None:
    if isinstance(value, list):
        value = value[0] if value else None
    if isinstance(value, dict):
        return value.get("url") or value.get("contentUrl")
    return _as_text(value) or None


def _walk_json_ld(data):
    if isinstance(data, list):
        for item in data:
            yield from _walk_json_ld(item)
    elif isinstance(data, dict):
        yield data
        graph = data.get("@graph")
        if graph is not None:
            yield from _walk_json_ld(graph)


def _json_ld_book(soup: BeautifulSoup) -> dict | None:
    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            continue
        for obj in _walk_json_ld(data):
            types = obj.get("@type")
            types = types if isinstance(types, list) else [types]
            if any(str(kind).lower() == "book" for kind in types):
                return obj
    return None


def _text(element) -> str:
    return element.get_text(" ", strip=True) if element is not None else ""


def _attr(element, name: str) -> str | None:
    if element is None:
        return None
    value = element.get(name)
    return value.strip() if isinstance(value, str) else None


def _inner_html(element) -> str:
    return element.decode_contents() if element is not None else ""


def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str:
    for selector in selectors:
        found = soup.select_one(selector)
        if found is not None and _text(found):
            return _text(found)
    return ""


def _text_list(soup: BeautifulSoup, selectors: list[str]) -> list[str]:
    for selector in selectors:
        values = [_text(node) for node in soup.select(selector)]
        values = [value for value in values if value]
        if values:
            return values
    return []


def parse_goodreads_html(html: str) -> ParsedBook:
    """Extract a book's fields from a Goodreads book page."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    authors: list[str] = []
    release_date: date | None = None
    blurb = ""
    image_url: str | None = None
    tags: list[str] = []

    book = _json_ld_book(soup)
    if book:
        title = _as_text(book.get("name"))
        authors = _author_names(book.get("author"))
        release_date = parse_date_text(_as_text(book.get("datePublished")))
        image_url = _image_url(book.get("image"))
        blurb = html_to_markdown(_as_text(book.get("description")))
        tags = _as_text_list(book.get("genre"))

    if not title:
        title = _first_text(
            soup,
            [
                ".BookPageTitleSection__title h1",
                "[data-testid='bookTitle']",
                "h1#bookTitle",
            ],
        )
    if not authors:
        authors = _text_list(
            soup,
            [
                ".ContributorLinksList span.ContributorLink__name",
                "[data-testid='name']",
            ],
        )
    if not image_url:
        image_url = _attr(
            soup.select_one(".BookCard__cover img")
            or soup.select_one("img.ResponsiveImage"),
            "src",
        )
    if not blurb:
        blurb = html_to_markdown(
            _inner_html(
                soup.select_one(".BookPageMetadataSection__description .Formatted")
            )
        )
    if not tags:
        tags = _text_list(
            soup,
            [
                ".BookPageMetadataSection__genreButton",
                ".BookPageMetadataSection__genres span",
            ],
        )

    blurb = _condense_newlines(blurb)

    if release_date is None:
        release_date = parse_date_text(
            _text(soup.select_one(".PreReleaseDetails"))
        )
    if release_date is None:
        raise BookScrapeError("releases.errors.no_release_date")

    return ParsedBook(
        title=title or t("releases.embed.untitled_book"),
        authors=authors,
        release_date=release_date,
        blurb=blurb,
        image_url=image_url,
        tags=tags,
    )


def parse_netgalley_html(html: str) -> ParsedBook:
    """Extract a book's fields from a NetGalley book page."""
    soup = BeautifulSoup(html, "html.parser")

    title = ""
    authors: list[str] = []
    release_date: date | None = None
    blurb = ""
    image_url: str | None = None
    tags: list[str] = []

    book = _json_ld_book(soup)
    if book:
        title = _as_text(book.get("name"))
        authors = _author_names(book.get("author"))
        release_date = parse_date_text(_as_text(book.get("datePublished")))
        image_url = _image_url(book.get("image"))
        blurb = html_to_markdown(_as_text(book.get("description")))
        tags = _as_text_list(book.get("genre"))

    if not title:
        title = _first_text(
            soup, ["[itemtype$='schema.org/Book'] h1.heading-1", "h1.heading-1"]
        )
    if not authors:
        authors = _text_list(
            soup,
            [
                "[itemprop='author'] [itemprop='name']",
                "[itemtype$='schema.org/Person'] span[itemprop='name']",
                ".contributors span",
            ],
        )
    if not image_url:
        image_url = _attr(
            soup.select_one("img.cover-width[itemprop='image']")
            or soup.select_one("img.cover-width"),
            "src",
        )
    if not blurb:
        blurb = html_to_markdown(_inner_html(soup.select_one("#descriptionFullTextBox")))
    if not tags:
        tags = _text_list(soup, [".categories a", "[itemprop='genre']"])

    blurb = _condense_newlines(blurb)

    if release_date is None:
        release_date = parse_date_text(_text(soup.select_one(".publication-dates")))
    if release_date is None:
        raise BookScrapeError("releases.errors.no_release_date")

    return ParsedBook(
        title=title or t("releases.embed.untitled_book"),
        authors=authors,
        release_date=release_date,
        blurb=blurb,
        image_url=image_url,
        tags=tags,
    )


_PARSERS = {
    "goodreads": parse_goodreads_html,
    "netgalley": parse_netgalley_html,
}


def parse_book_html(source: str, html: str) -> ParsedBook:
    """Parse page HTML for ``source`` (``goodreads``/``netgalley``)."""
    try:
        parser = _PARSERS[source]
    except KeyError as exc:
        raise BookScrapeError("releases.errors.unsupported_source") from exc
    return parser(html)


def build_book_data(
    url: str, source: str, source_id: str, parsed: ParsedBook
) -> BookData:
    return BookData(
        title=parsed.title,
        authors=parsed.authors,
        release_date=parsed.release_date,
        blurb=parsed.blurb,
        image_url=parsed.image_url,
        tags=parsed.tags,
        source=source,
        source_id=source_id,
        url=url,
    )


def book_data_to_dict(data: BookData) -> dict:
    """Serialise ``BookData`` for storage in a draft row."""
    return {
        "title": data.title,
        "authors": data.authors,
        "release_date": data.release_date.isoformat(),
        "blurb": data.blurb,
        "image_url": data.image_url,
        "tags": data.tags,
        "source": data.source,
        "source_id": data.source_id,
        "url": data.url,
    }


def book_data_from_dict(values: dict) -> BookData:
    """Rebuild ``BookData`` from a stored draft row."""
    return BookData(
        title=values["title"],
        authors=list(values.get("authors") or []),
        release_date=date.fromisoformat(values["release_date"]),
        blurb=values.get("blurb") or "",
        image_url=values.get("image_url"),
        tags=list(values.get("tags") or []),
        source=values["source"],
        source_id=values["source_id"],
        url=values["url"],
    )


class ReleaseScraper:
    """Drives one shared headless Chromium to fetch book pages."""

    def __init__(self, *, headless: bool = True, timeout_ms: int = 30000) -> None:
        self._headless = headless
        self._timeout_ms = timeout_ms
        self._playwright = None
        self._browser = None
        self._context = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        """Launch the browser. Idempotent; safe to call at startup."""
        if self._context is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BookScrapeError("releases.errors.browser_missing") from exc

        try:
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=self._headless
            )
            self._context = await self._browser.new_context(
                locale="en-US",
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
        except Exception as exc:
            logger.error(
                "Could not launch the release scraper browser: %s", exc
            )
            await self.close()
            raise BookScrapeError("releases.errors.browser_missing") from exc

    async def close(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                try:
                    await closer.close()
                except Exception:  # pragma: no cover - best effort teardown
                    logger.debug("Error closing scraper resource", exc_info=True)
        if self._playwright is not None:
            try:
                await self._playwright.stop()
            except Exception:  # pragma: no cover - best effort teardown
                logger.debug(
                    "Error stopping Playwright", exc_info=True
                )
        self._context = None
        self._browser = None
        self._playwright = None

    async def fetch(self, url: str) -> BookData:
        """Fetch and parse a book page, returning normalised data.

        Transient page-load failures are retried a few times, mirroring the old
        bot. Permanent outcomes (unsupported URL, missing release date) fail
        straight away so we don't waste browser loads or hide the real reason.
        """
        source, source_id = parse_source_url(url)
        for attempt in range(1, _SCRAPE_ATTEMPTS + 1):
            try:
                async with self._lock:
                    await self.start()
                    html = await self._load_html(url, source)
                parsed = parse_book_html(source, html)
                return build_book_data(url, source, source_id, parsed)
            except BookScrapeError as exc:
                if exc.key not in _RETRYABLE_KEYS or attempt == _SCRAPE_ATTEMPTS:
                    raise
                logger.warning(
                    "Retrying scrape of %s (attempt %s/%s) after %s",
                    url,
                    attempt,
                    _SCRAPE_ATTEMPTS,
                    exc.key,
                )
                await asyncio.sleep(2 ** (attempt - 1))
        raise AssertionError("unreachable")  # pragma: no cover

    async def _load_html(self, url: str, source: str) -> str:
        from playwright.async_api import Error as PlaywrightError

        page = await self._context.new_page()
        try:
            await page.goto(
                url, wait_until="domcontentloaded", timeout=self._timeout_ms
            )
            if source == "netgalley":
                await self._dismiss_consent(page)

            # Wait for the page's content, not just its shell, so we don't read
            # a half-rendered page and then report a missing release date.
            selector = (
                "#page-content"
                if source == "netgalley"
                else "[data-testid='bookTitle'], .BookPageTitleSection__title, main.BookPage"
            )
            try:
                await page.wait_for_selector(selector, timeout=15000)
            except PlaywrightError:
                pass
            return await page.content()
        except BookScrapeError:
            raise
        except PlaywrightError as exc:
            raise BookScrapeError("releases.errors.scrape_failed") from exc
        finally:
            try:
                await page.close()
            except PlaywrightError:
                pass

    async def _dismiss_consent(self, page) -> None:
        from playwright.async_api import Error as PlaywrightError

        try:
            await page.locator(
                '[data-testid="uc-accept-all-button"]'
            ).click(timeout=3000)
        except PlaywrightError:
            pass
