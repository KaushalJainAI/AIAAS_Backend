"""
Tools that reach the open web.

Every one of these hands third-party bytes to the model, so each caps what it
returns: an unbounded page or research run would spend the context window on
boilerplate. `chat.tool_output` is the backstop above these budgets, not a
replacement for them.
"""
from __future__ import annotations

import asyncio
import json
import logging

from typing import Any, Dict

from core.safety.net import UnsafeURLError, fetch_url

from workflow_backend.thresholds import (
    READ_URL_CHAR_LIMIT,
)

from .registry import tool

logger = logging.getLogger(__name__)

#: Total extracted text handed back from one deep_research call. Past this
#: the model starts losing the earlier sources anyway, and the context clamp
#: would trim it blindly rather than by relevance.
DEEP_RESEARCH_CHAR_BUDGET = 60_000


@tool({
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web to find up-to-date information, news, facts, or references.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The search query to execute on the web."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    })
async def web_search(args: Dict, context: Dict) -> str:
    from chat.sources.search import web_search

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required."
    results = await web_search(query)
    return json.dumps({
        "type": "search_results",
        "text": (
            f"Search results for '{query}':\n\n{results.text}" if results.text
            else f"No results found for '{query}'. Try different wording."
        ),
        "sources": list(results.sources),
    })


@tool({
        "type": "function",
        "function": {
            "name": "deep_research",
            "description": "Research a topic in depth: plans several search queries, runs them, reads the resulting pages and returns their extracted text with sources. Use this instead of repeated web_search calls when the question needs breadth or corroboration across sources.",
            "parameters": {
                "type": "object",
                "properties": {
                    "topic": {
                        "type": "string",
                        "description": "The subject to research, stated in full."
                    },
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "string"
                        },
                        "description": "2-4 distinct search queries covering different angles. Omit to derive them from the topic."
                    },
                    "max_pages": {
                        "type": "integer",
                        "description": "Pages to read, 5-50. Defaults to 15."
                    }
                },
                "required": [
                    "topic"
                ],
                "additionalProperties": False
            }
        }
    })
async def deep_research(args: Dict, context: Dict) -> str:
    """
    Breadth-first research: fan out across queries, read the pages, return
    the text with its sources.

    This is the pipeline that used to be inlined in the streaming view and
    ran ahead of the model on a keyword guess. As a tool the model decides
    when the question actually warrants it, and can follow up on what comes
    back instead of being handed one fixed synthesis prompt.
    """
    from chat.sources.search import scrape_sources, web_search

    topic = (args.get("topic") or "").strip()
    if not topic:
        return "Error: 'topic' is required."

    queries = [str(q).strip() for q in (args.get("queries") or []) if str(q).strip()]
    queries = queries[:4] or [topic]

    try:
        max_pages = int(args.get("max_pages", 15))
    except (TypeError, ValueError):
        max_pages = 15
    max_pages = max(5, min(max_pages, 50))

    results = await asyncio.gather(*(web_search(q, max_results=10) for q in queries))

    by_url: Dict[str, Dict[str, Any]] = {}
    for result in results:
        for source in result.sources:
            if source.get("url"):
                by_url.setdefault(source["url"], source)

    blocks, usable = await scrape_sources(list(by_url.values())[:max_pages])
    if not usable:
        return json.dumps({
            "type": "deep_research",
            "text": (
                f"Searched {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
                f"for '{topic}' but could not read any of the pages found. "
                f"Try web_search with narrower terms."
            ),
            "queries": queries,
            "sources": [],
        })

    corpus = "\n\n".join(blocks)[:DEEP_RESEARCH_CHAR_BUDGET]
    return json.dumps({
        "type": "deep_research",
        "text": (
            f"Deep research on '{topic}'.\nQueries: {queries}\n"
            f"Pages read: {len(usable)}\n\n{corpus}"
        ),
        "queries": queries,
        "sources": usable,
    })


@tool({
        "type": "function",
        "function": {
            "name": "image_search",
            "description": "Search for specific images visually related to a topic. Run this if the user asks to see photos, diagrams, or visual examples.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The image search query."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    })
async def image_search(args: Dict, context: Dict) -> str:
    from chat.sources.search import image_search

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required."
    images = await image_search(query)
    return json.dumps({
        "type": "image_results",
        "text": f"Retrieved {len(images)} image(s) for '{query}'; they are shown in the UI.",
        "images": images,
    })


@tool({
        "type": "function",
        "function": {
            "name": "video_search",
            "description": "Search for specific videos related to a topic. Run this if the user asks to see videos, tutorials, or footage.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "The video search query."
                    }
                },
                "required": [
                    "query"
                ],
                "additionalProperties": False
            }
        }
    })
async def video_search(args: Dict, context: Dict) -> str:
    from chat.sources.search import video_search

    query = (args.get("query") or "").strip()
    if not query:
        return "Error: 'query' is required."
    videos = await video_search(query)
    return json.dumps({
        "type": "video_results",
        "text": f"Retrieved {len(videos)} video(s) for '{query}'; they are shown in the UI.",
        "videos": videos,
    })


@tool({
        "type": "function",
        "function": {
            "name": "read_url",
            "description": "Fetch and extract text content from a given web page URL.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL to fetch."
                    }
                },
                "required": [
                    "url"
                ],
                "additionalProperties": False
            }
        }
    })
async def read_url(args: Dict, context: Dict) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: Missing URL"
    try:
        # `fetch_url` validates every redirect hop, not just this URL, and
        # runs in a thread because urllib blocks — called inline it stalls
        # the whole ASGI loop and every other chat stream stops mid-token.
        html = await asyncio.to_thread(fetch_url, url, timeout=10)
    except UnsafeURLError as e:
        return json.dumps({"error": f"URL blocked: {e}"})
    except Exception as e:
        return json.dumps({"error": f"Failed to read URL '{url}': {str(e)}"})

    try:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(html, 'html.parser').get_text(separator=' ', strip=True)
    except ImportError:
        text = html.decode('utf-8', errors='ignore')
    return json.dumps({"url": url, "content": text[:READ_URL_CHAR_LIMIT]})


# -- scrape_webpage extractors ------------------------------------------------
#
# One function per `extract` key. These were six inline `if "x" in
# extract_types:` blocks inside a single 87-line function; separated, each cap
# and truncation sits next to the thing it bounds.

_MAX_HEADINGS = 50
_MAX_LINKS = 100
_MAX_TABLES = 5
_MAX_TABLE_ROWS = 30
_MAX_IMAGES = 20


def _extract_metadata(soup) -> dict:
    meta = {
        "title": soup.title.string.strip() if soup.title and soup.title.string else "",
        "description": "",
        "og_image": "",
    }
    desc = soup.find("meta", attrs={"name": "description"})
    if desc:
        meta["description"] = desc.get("content", "")[:500]
    og_img = soup.find("meta", attrs={"property": "og:image"})
    if og_img:
        meta["og_image"] = og_img.get("content", "")
    return meta


def _extract_headings(soup) -> list:
    headings = []
    for level in range(1, 7):
        for h in soup.find_all(f"h{level}"):
            text = h.get_text(strip=True)
            if text:
                headings.append({"level": level, "text": text[:200]})
    return headings[:_MAX_HEADINGS]


def _extract_links(soup) -> list:
    links = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href and not href.startswith(("#", "javascript:")):
            links.append({"text": a.get_text(strip=True)[:100], "href": href})
    return links[:_MAX_LINKS]


def _extract_tables(soup) -> list:
    tables = []
    for table in soup.find_all("table")[:_MAX_TABLES]:
        rows = []
        for tr in table.find_all("tr")[:_MAX_TABLE_ROWS]:
            cells = [td.get_text(strip=True)[:200] for td in tr.find_all(["td", "th"])]
            if cells:
                rows.append(cells)
        if rows:
            tables.append(rows)
    return tables


def _extract_images(soup) -> list:
    images = []
    for img in soup.find_all("img", src=True)[:_MAX_IMAGES]:
        src = img.get("src", "")
        if src:
            images.append({"src": src, "alt": img.get("alt", "")[:100]})
    return images


def _extract_text(soup) -> str:
    """Destructive: strips chrome elements out of `soup` before reading it.

    Must run after every other extractor -- see the loop in `scrape_webpage`.
    """
    for tag in soup(["script", "style", "nav", "footer", "header"]):
        tag.decompose()
    return soup.get_text(separator="\n", strip=True)[:READ_URL_CHAR_LIMIT]


#: Order is load-bearing: `text` last, because it mutates the tree.
_EXTRACTORS = {
    "metadata": _extract_metadata,
    "headings": _extract_headings,
    "links": _extract_links,
    "tables": _extract_tables,
    "images": _extract_images,
    "text": _extract_text,
}


@tool({
        "type": "function",
        "function": {
            "name": "scrape_webpage",
            "description": "Scrape a webpage and extract structured content including headings, links, tables, and metadata. More powerful than read_url — use this when you need structured data from a page, not just raw text.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "The URL of the webpage to scrape."
                    },
                    "extract": {
                        "type": "array",
                        "items": {
                            "type": "string",
                            "enum": [
                                "text",
                                "headings",
                                "links",
                                "tables",
                                "metadata",
                                "images"
                            ]
                        },
                        "description": "What to extract from the page (default: all). Specify a subset to reduce output size."
                    }
                },
                "required": [
                    "url"
                ],
                "additionalProperties": False
            }
        }
    })
async def scrape_webpage(args: Dict, context: Dict) -> str:
    url = args.get("url", "")
    if not url:
        return "Error: Missing URL"
    extract_types = args.get("extract", list(_EXTRACTORS))
    # A model can pass a bare string here; `"metadata" in "text"` would then
    # silently answer the wrong question.
    if isinstance(extract_types, str):
        extract_types = [extract_types]
    try:
        html_bytes = await asyncio.to_thread(fetch_url, url, timeout=15)
    except UnsafeURLError as e:
        return json.dumps({"error": f"URL blocked: {e}"})
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Failed to scrape '{url}': {str(e)}"})

    try:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html_bytes, 'html.parser')
        except ImportError:
            text = html_bytes.decode('utf-8', errors='ignore')[:READ_URL_CHAR_LIMIT]
            return json.dumps({"url": url, "text": text, "error": "BeautifulSoup not installed, returning raw text"})

        # Iterated in registry order, not in the order the caller listed them.
        # `_extract_text` decomposes nav/header/footer out of the tree, so it
        # has to run last or the links and headings inside those elements
        # would vanish from an answer that asked for both.
        result = {"url": url}
        for key, extract in _EXTRACTORS.items():
            if key in extract_types:
                result[key] = extract(soup)

        return json.dumps({"status": "success", **result})
    except Exception as e:
        return json.dumps({"status": "error", "error": f"Failed to scrape '{url}': {str(e)}"})
