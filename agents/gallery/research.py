"""
The `research` pack: sourced research three ways.

A report (Deep research), a comparison workbook (Competitor analysis), or a
page you can share by link (Report publisher).

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['deep-research', 'competitor-analysis', 'report-publisher']


RESEARCH_PROMPT = """\
You research a topic in depth and report what you actually found.

How to work:
- Break the topic into 2-4 distinct angles and search each one. Different
  angles, not rephrasings of the same query.
- Read the pages you find. A search snippet is not a source; open it.
- Corroborate anything load-bearing across at least two independent pages.
- When sources disagree, say so and say which you find more credible and why.
  Do not average them into a claim neither one makes.
- Never state a fact you did not read. If you could not find something, say
  that you could not find it.
"""


PUBLISHER_PROMPT = """\
You research a topic and publish the result as a web page people can open.

How to work:
- Research first: search from several angles, open the pages you rely on, and
  keep the source URL for every claim.
- Write the report in markdown: a one-paragraph summary, then sections, then
  sources as links. Put charts in fenced ```chart blocks holding the same JSON
  render_chart takes (kind, title, series).
- Publish with publish_page as kind "report". Use the visibility the user asked
  for; if they did not say, use "link" — the narrowest that works.
- Reply with the page link and one sentence on what it covers.
"""


COMPETITOR_PROMPT = """\
You compare competitors and hand back a comparison people can use.

How to work:
- Pin down the set first: which companies, and which dimensions (pricing,
  features, audience, positioning). Ask if the user named neither.
- Research each company from its own site and at least one independent source;
  keep the URL for every fact, and mark anything you could not verify.
- Build a workbook with render_workbook: one row per company, one column per
  dimension, sources in the last column.
- If asked for a presentation, build a short deck with render_deck: the
  landscape, where each player is strong, the gaps, and what it means.
- Never present a guess as a fact; "not published" is an honest answer.
"""


TEMPLATES: dict[str, dict[str, Any]] = {
    'deep-research': {
        'name': 'Deep research',
        'tagline': 'Researches a topic across several angles and reports with sources.',
        'description': (
            'Breaks a topic into distinct angles, searches each one, opens the '
            'pages it finds, and reports what it actually read — with the '
            'disagreements between sources left visible rather than averaged '
            'away. Reads the public web and nothing of yours, so it needs no '
            'connections and can run unattended.'
        ),
        'icon': 'search',
        'tags': ['research', 'web'],
        'requirements': [],
        'config': {
            'name': 'Deep research',
            'brief': RESEARCH_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True},
            'fileAccess': 'none',
            # Nothing it touches is yours and nothing it does is irreversible,
            # so there is no question worth stopping to ask.
            'autonomy': 'full',
            'spendCapRupees': 500,
        },
    },

    'report-publisher': {
        'name': 'Report publisher',
        'tagline': 'Researches a topic and publishes it as a shareable page.',
        'description': (
            'Researches a topic across several sources and publishes the result '
            'as a web page with charts and links, ready to send to someone. '
            'Pauses before anything is published, and defaults to link-only.'
        ),
        'icon': 'globe',
        'tags': ['research', 'publishing', 'web'],
        'requirements': [],
        'config': {
            'name': 'Report publisher',
            'brief': PUBLISHER_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'publish': True},
            'fileAccess': 'none',
            # `auto`: research runs freely; publishing is irreversible, so it
            # stops for a human every time.
            'autonomy': 'auto',
            'spendCapRupees': 500,
        },
    },

    'competitor-analysis': {
        'name': 'Competitor analysis',
        'tagline': 'Compares competitors in a sourced workbook and a short deck.',
        'description': (
            'Researches the companies you name across pricing, features and '
            'positioning, and returns a comparison workbook with a source for '
            'every fact — plus a short deck if you ask for one.'
        ),
        'icon': 'swords',
        'tags': ['research', 'strategy', 'office'],
        'requirements': [],
        'config': {
            'name': 'Competitor analysis',
            'brief': COMPETITOR_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 600,
            'outputContract': 'files',
        },
    },
}
