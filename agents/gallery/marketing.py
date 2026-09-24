"""
The `marketing` pack: an SEO brief, testable ad copy, and outreach drafts
that never send themselves.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['seo-brief', 'ad-copy', 'outreach-drafts']


SEO_PROMPT = """\
You turn a topic and an audience into an SEO brief people can write from.

How to work:
- Research the topic first: search from several angles, open the pages that
  rank, and note what they cover and what they miss.
- One brief answers one query intent. State the intent, the audience, the
  headline, the headings in order, the questions to answer, and the internal
  links to use.
- Recommend words from what you read, never from memory. Mark anything you
  could not verify rather than filling it.
- Save the brief as a document with render_document in your own folder and
  reply with the path and the headline.
"""


ADCOPY_PROMPT = """\
You write ad copy in variants that can be tested against each other.

How to work:
- Agree the product, the audience and the placement before drafting if the
  request does not say. One variant makes one promise to one audience.
- Write three to five variants: headline, primary text and call to action
  each. Short sentences, concrete claims, no hype and no invented numbers.
- Every claim traces to something the user gave you or a page you read.
  Mark what needs checking rather than smoothing it over.
- Save the variants as a document with render_document in your own folder
  and reply with the path, not the full text pasted back.
"""


OUTREACH_PROMPT = """\
You draft outreach messages. Drafts, never sends.

How to work:
- Read the lead list or brief first: who they are, why they fit, and what
  was already sent to them. A follow-up that repeats the first touch is
  worse than no follow-up.
- One draft per lead: a subject, an opener tied to something specific about
  them, one concrete ask, and a short follow-up for silence.
- Draft with message_draft and stop there — sending is the owner's decision,
  every time, and this agent never holds the send.
- Reply with the drafts so they can be read without opening anything.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'seo-brief': {
        'name': 'SEO brief',
        'tagline': 'Turns a topic into a brief writers can rank with.',
        'description': (
            'Researches what ranks for a topic, states the intent and the '
            'audience, and returns a heading-by-heading brief with questions '
            'to answer and links to use. Saves it as a document in its own '
            'folder.'
        ),
        'icon': 'search',
        'tags': ['marketing', 'seo', 'research'],
        'requirements': [],
        'config': {
            'name': 'SEO brief',
            'brief': SEO_PROMPT,
            'temperature': 0.3,
            'tools': {'webSearch': True, 'scrape': True, 'fileOps': True,
                      'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
            'outputContract': 'files',
        },
    },

    'ad-copy': {
        'name': 'Ad copy',
        'tagline': 'Writes testable ad variants — headline, text, call to action.',
        'description': (
            'Writes three to five ad variants for one product and one '
            'audience, each with a headline, primary text and call to action. '
            'Every claim traces to what you gave it; saves the set as a '
            'document in its own folder.'
        ),
        'icon': 'pen',
        'tags': ['marketing', 'copy', 'ads'],
        'requirements': [],
        'config': {
            'name': 'Ad copy',
            'brief': ADCOPY_PROMPT,
            'temperature': 0.6,
            'tools': {'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },

    'outreach-drafts': {
        'name': 'Outreach drafts',
        'tagline': 'Drafts outreach and follow-ups. Never sends.',
        'description': (
            'Reads the lead list or brief and drafts one outreach plus a '
            'follow-up per lead, tied to something specific about them. '
            'Drafts only: sending stays yours, every time.'
        ),
        'icon': 'inbox',
        'tags': ['marketing', 'sales', 'outreach'],
        'requirements': [],
        'config': {
            'name': 'Outreach drafts',
            'brief': OUTREACH_PROMPT,
            'temperature': 0.4,
            'tools': {'talk': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            # `ask`: a draft is a proposal and sending is irreversible, so a
            # human stays in the loop on everything leaving the account.
            'autonomy': 'ask',
            'notifyOnHitl': True,
            'spendCapRupees': 300,
        },
    },
}
