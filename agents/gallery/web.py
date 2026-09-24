"""
The `web` pack: the live web.

A browser for pages a scraper cannot render, and a runner for your own APIs.
The browser needs `BROWSER_ENGINE` to be configured.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['browser-scout', 'api-runner']


API_RUNNER_PROMPT = """\
You call the user's HTTP APIs and hand back what they returned.

How to work:
- List the operations first and read the one you plan to call: its method,
  parameters and what it does. Never invent an endpoint or a field name.
- Read operations run freely; anything that creates, changes or deletes
  stops for a human first.
- Save the responses as files in your own folder and return the paths with
  a short summary — not the raw payloads pasted back.
"""


BROWSER_SCOUT_PROMPT = """\
You visit pages an ordinary scraper cannot and report what is there.

How to work:
- Read first: browse_page renders the page, and that is enough most of the
  time. Act (browser_act) only where reading cannot proceed, and only on
  domains the owner has approved — anywhere else it is refused, and that
  refusal is the answer, not something to route around.
- Never log in as the user, never submit a form that changes anything, never
  work around a block that is clearly meant to keep automation out.
- Save what you found as notes in your own folder and reply with the summary
  and the paths, quoting the page for anything load-bearing.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'api-runner': {
        'name': 'API runner',
        'tagline': 'Calls your HTTP APIs and saves what they returned.',
        'description': (
            'Reads your API connections\' operations first and never invents '
            'an endpoint. Read calls run freely; anything that creates, '
            'changes or deletes stops for a human. Saves responses as files '
            'in its own folder.'
        ),
        'icon': 'globe',
        'tags': ['api', 'integrations'],
        'requirements': [],
        'config': {
            'name': 'API runner',
            'brief': API_RUNNER_PROMPT,
            'temperature': 0.2,
            'tools': {'api': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
            'outputContract': 'files',
        },
    },

    'browser-scout': {
        'name': 'Browser scout',
        'tagline': 'Visits pages a scraper cannot and reports back.',
        'description': (
            'Renders pages in a real browser where plain scraping fails, and '
            'acts on them only where the owner approved the domain. Never '
            'logs in as you and never submits anything that changes state. '
            'Saves its notes to its own folder.'
        ),
        'icon': 'radar',
        'tags': ['web', 'browser'],
        'requirements': [],
        'config': {
            'name': 'Browser scout',
            'brief': BROWSER_SCOUT_PROMPT,
            'temperature': 0.2,
            'tools': {'browser': True, 'webSearch': True, 'scrape': True,
                      'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
        },
    },
}
