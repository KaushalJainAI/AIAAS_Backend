"""
Web, image and video search plus page fetching.

Lifted out of `views.py`, where it forced `tools.py` to import from the view
layer to run a search. The three DuckDuckGo searches were also three copies of
the same import-retry-thread dance; they now share `_ddgs`.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Callable, Iterable
from urllib.parse import urlparse

from workflow_backend.thresholds import (
    IMAGE_SEARCH_MAX_RESULTS,
    SEARCH_RESULT_LIMIT,
    VIDEO_SEARCH_MAX_RESULTS,
)

logger = logging.getLogger(__name__)

_FETCH_TIMEOUT = 5
_SEARCH_ATTEMPTS = 2


@dataclass(frozen=True, slots=True)
class WebResults:
    """Search results in both the shapes callers need."""

    #: Numbered text block for the model to read.
    text: str = ""
    #: Structured records for the citation UI.
    sources: tuple[dict[str, Any], ...] = ()


def _ddgs(method: str, query: str, *, max_results: int, timeout: int) -> list[dict]:
    """
    Run one DuckDuckGo query synchronously, retrying once on transport errors.

    Called via `asyncio.to_thread`; the client is blocking.
    """
    try:
        from ddgs import DDGS
    except ImportError:  # package was renamed; older installs still work
        from duckduckgo_search import DDGS  # type: ignore[import-not-found]

    for attempt in range(1, _SEARCH_ATTEMPTS + 1):
        try:
            with DDGS(timeout=timeout) as client:
                results = list(getattr(client, method)(query, max_results=max_results) or [])
            if results:
                return results
        except Exception as exc:  # the client raises a wide range of transport errors
            logger.warning(
                "[Search] %s attempt %d/%d failed for %r: %s",
                method, attempt, _SEARCH_ATTEMPTS, query[:80], exc,
            )
    return []


async def _search(method: str, query: str, *, max_results: int, timeout: int) -> list[dict]:
    if not query.strip():
        return []
    results = await asyncio.to_thread(
        _ddgs, method, query, max_results=max_results, timeout=timeout
    )
    logger.info("[Search] %s(%r) → %d result(s)", method, query[:80], len(results))
    return results


def _favicon(domain: str) -> str:
    return f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else ""


def _domain(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except ValueError:
        return ""


_THUMBNAIL_KEYS = ("thumbnail", "image", "icon", "favicon", "photo", "img")


async def web_search(query: str, max_results: int = SEARCH_RESULT_LIMIT) -> WebResults:
    """Search the web. Returns model-readable text alongside source records."""
    raw = await _search("text", query, max_results=max_results, timeout=15)

    lines: list[str] = []
    sources: list[dict[str, Any]] = []
    for position, item in enumerate(raw, start=1):
        url = item.get("href") or item.get("url") or ""
        body = item.get("body") or ""
        if not url and not body:
            continue

        title = item.get("title") or "Untitled"
        domain = _domain(url)
        lines.append(f"[{position}] {title}\n{body}")
        sources.append({
            "title": title,
            "url": url,
            "snippet": body,
            "source": domain,
            "publisher": domain,
            "thumbnail": next(
                (item[k] for k in _THUMBNAIL_KEYS if item.get(k)), ""
            ),
            "favicon": item.get("favicon") or _favicon(domain),
        })

    return WebResults(text="\n\n".join(lines), sources=tuple(sources))


async def image_search(
    query: str, max_results: int = IMAGE_SEARCH_MAX_RESULTS
) -> list[dict[str, Any]]:
    """Search for images."""
    raw = await _search("images", query, max_results=max_results, timeout=10)
    return [
        {
            "title": item.get("title", ""),
            "image": item.get("image", ""),
            "url": item.get("url", ""),
            "source": item.get("source", ""),
        }
        for item in raw
    ]


def _video_url(raw_url: str) -> str:
    """DuckDuckGo returns bare YouTube ids for some results."""
    if raw_url and not raw_url.startswith("http"):
        return f"https://www.youtube.com/watch?v={raw_url}"
    return raw_url


async def video_search(
    query: str, max_results: int = VIDEO_SEARCH_MAX_RESULTS
) -> list[dict[str, Any]]:
    """Search for videos."""
    raw = await _search("videos", query, max_results=max_results, timeout=10)
    return [
        {
            "title": item.get("title", ""),
            "description": item.get("description", ""),
            "url": _video_url(item.get("content") or ""),
            "duration": item.get("duration", ""),
            "publisher": item.get("publisher", ""),
        }
        for item in raw
    ]


# ── Page fetching ────────────────────────────────────────────────────────────

def _fetch_text(url: str, char_limit: int) -> str:
    """Fetch one page and return its visible text. Empty string on any failure."""
    from core.safety.net import UnsafeURLError, fetch_url

    try:
        # fetch_url re-runs the SSRF guard on every redirect hop; a plain
        # urlopen validates only the first URL and then follows a 302 to
        # 169.254.169.254 unchecked, which is the exact hole this closes.
        html = fetch_url(url, timeout=_FETCH_TIMEOUT)
    except UnsafeURLError as exc:
        logger.info("[Scrape] Refused %s: %s", url, exc)
        return ""
    except Exception as exc:  # urllib raises OSError, HTTPError, ssl errors, …
        logger.info("[Scrape] %s failed: %s", url, exc)
        return ""

    try:
        from bs4 import BeautifulSoup

        return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)[:char_limit]
    except ImportError:
        return html.decode("utf-8", errors="ignore")[:char_limit]


async def scrape_sources(
    sources: Iterable[dict[str, Any]],
    *,
    per_source_chars: int = 4_000,
    min_chars: int = 100,
) -> tuple[list[str], list[dict[str, Any]]]:
    """
    Fetch each source's page in parallel.

    Returns the extracted text blocks and the sources that actually yielded
    content, so callers never cite a page that turned out to be unreadable.
    """
    sources = [s for s in sources if s.get("url")]
    if not sources:
        return [], []

    texts = await asyncio.gather(
        *(asyncio.to_thread(_fetch_text, s["url"], per_source_chars) for s in sources),
        return_exceptions=True,
    )

    blocks: list[str] = []
    usable: list[dict[str, Any]] = []
    for source, text in zip(sources, texts):
        if isinstance(text, str) and len(text) > min_chars:
            blocks.append(f"Source [{source.get('title', 'Untitled')}]({source['url']}):\n{text}")
            usable.append(source)
    return blocks, usable
