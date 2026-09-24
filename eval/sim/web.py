"""
Simulated web: a frozen corpus of pages plus fixed search results.

Fixture schema (`fixtures['web']`):

    {"pages": [{"url": "https://acme.test/pricing",
                "title": "Acme pricing",
                "text": "The Pro plan costs $49 per seat per month. …"}],
     "results": {"acme pricing": ["https://acme.test/pricing"]}}

`results` maps a normalised query to the urls it returns, in order — the
frozen equivalent of a search engine. Lookup: exact normalised match first,
then the first key contained in the query; anything else is "no results",
never a live fetch. `pages` urls not named by any query are reachable only
by direct `read_url`, the way an unlinked page is.

Result shapes mirror `chat/tools/web.py`: searches answer
`{"type": "search_results", "text", "sources": [{title, url}]}`, reads answer
`{"url", "content"}`, scrapes answer `{"status": "success", "url", …}` with
the requested extract keys. An unknown query is the tool's own no-results
sentence; an unknown url is an `{"error": …}` / `{"status": "error", …}` in
the tool's own shape. Frozen means a research case scores the same a week
apart — the property E-4 exists for.
"""
from __future__ import annotations

import json

TOOLS = (
    'web_search',
    'read_url',
    'scrape_webpage',
)

REQUIRED_PAGE_KEYS = ('url', 'title', 'text')

_SCRAPE_KEYS = ('metadata', 'headings', 'links', 'tables', 'images', 'text')


def _norm_query(query: str) -> str:
    return ' '.join(str(query or '').lower().split())


def _norm_url(url: str) -> str:
    return str(url or '').strip().rstrip('/')


class WebSim:
    """One attempt's frozen web. Reset per attempt, never shared."""

    TOOLS = TOOLS

    def __init__(self, fixtures: dict | None = None):
        self.reset(fixtures or {})

    def reset(self, fixtures: dict) -> None:
        self.pages = [dict(p) for p in (fixtures.get('pages') or [])
                      if isinstance(p, dict)]
        self.results = {str(q).lower(): list(urls) for q, urls in
                        (fixtures.get('results') or {}).items()
                        if isinstance(urls, list)}
        self.queries: list[str] = []
        self.reads: list[str] = []

    # -- dispatch ------------------------------------------------------

    def handles(self, name: str) -> bool:
        return name in TOOLS

    def run(self, name: str, args: dict) -> str:
        try:
            handler = getattr(self, f'_run_{name}', None)
            if handler is None:
                return f"Error: '{name}' is not simulated in this evaluation world."
            return handler(args or {})
        except Exception as exc:  # noqa: BLE001 - a simulator never raises
            return f'Error: {exc}'

    def _page(self, url: str) -> dict | None:
        want = _norm_url(url)
        return next((p for p in self.pages if _norm_url(p.get('url')) == want),
                    None)

    def _urls_for(self, query: str) -> list[str]:
        norm = _norm_query(query)
        if norm in self.results:
            return self.results[norm]
        for key, urls in self.results.items():
            if key and key in norm:
                return urls
        return []

    # -- tools -----------------------------------------------------------

    def _run_web_search(self, args: dict) -> str:
        query = str(args.get('query') or '').strip()
        if not query:
            return "Error: 'query' is required."
        self.queries.append(query)
        urls = self._urls_for(query)[:10]
        by_url = {_norm_url(p.get('url')): p for p in self.pages}
        sources = [{'title': by_url[u]['title'], 'url': by_url[u].get('url')}
                   for u in (_norm_url(u) for u in urls) if u in by_url]
        if not sources:
            return json.dumps({
                'type': 'search_results',
                'text': f"No results found for '{query}'. Try different wording.",
                'sources': [],
            })
        lines = [f"{s['title']}\n{s['url']}\n"
                 f"{by_url[_norm_url(s['url'])].get('text', '')[:300]}"
                 for s in sources]
        return json.dumps({
            'type': 'search_results',
            'text': f"Search results for '{query}':\n\n" + '\n\n'.join(lines),
            'sources': sources,
        })

    def _run_read_url(self, args: dict) -> str:
        url = str(args.get('url') or '')
        if not url:
            return 'Error: Missing URL'
        page = self._page(url)
        if page is None:
            return json.dumps(
                {'error': f'No such page in this evaluation world: {url}.'})
        self.reads.append(url)
        return json.dumps({'url': url, 'content': str(page.get('text') or '')[:15000]})

    def _run_scrape_webpage(self, args: dict) -> str:
        url = str(args.get('url') or '')
        if not url:
            return 'Error: Missing URL'
        page = self._page(url)
        if page is None:
            return json.dumps({'status': 'error',
                               'error': f'No such page in this world: {url}.'})
        self.reads.append(url)
        wanted = args.get('extract') or list(_SCRAPE_KEYS)
        if isinstance(wanted, str):
            wanted = [wanted]
        text = str(page.get('text') or '')
        result: dict = {'url': url}
        if 'metadata' in wanted:
            result['metadata'] = {'title': page.get('title', ''),
                                  'description': text[:500], 'og_image': ''}
        if 'headings' in wanted:
            result['headings'] = [
                {'level': line.count('#', 0, 6) or 1,
                 'text': line.lstrip('# ').strip()[:200]}
                for line in text.splitlines() if line.startswith('#')][:50]
        if 'links' in wanted:
            result['links'] = []
        if 'tables' in wanted:
            result['tables'] = []
        if 'images' in wanted:
            result['images'] = []
        if 'text' in wanted:
            result['text'] = text[:15000]
        return json.dumps({'status': 'success', **result})

    # -- grading ----------------------------------------------------------

    def snapshot(self) -> dict:
        return {
            'pages': [{'url': p.get('url'), 'title': p.get('title')}
                      for p in self.pages],
            'queries': list(self.queries),
            'reads': list(self.reads),
        }

    def changes(self) -> dict:
        # Reads change nothing; what the agent asked is the reviewable trace.
        out: dict = {}
        if self.queries:
            out['searches'] = list(self.queries[:20])
        if self.reads:
            out['reads'] = list(self.reads[:20])
        return out

    def apply_expected(self, expect: dict) -> None:
        """Reads change nothing: no expected state to perform."""


__all__ = ['TOOLS', 'WebSim', 'REQUIRED_PAGE_KEYS']
