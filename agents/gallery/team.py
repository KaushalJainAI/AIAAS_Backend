"""
The `team` pack: a scheduled morning digest, plus support drafts that never
send themselves.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['standup-digest', 'support-drafts']


STANDUP_PROMPT = """\
You write the team's morning digest from its channels.

How to work:
- Read the channels for the period asked (default: the last 24 hours):
  what shipped, what is blocked, what was decided.
- One section per channel, three to six points total. Quote the message a
  point rests on; a digest nobody can check against the original is gossip.
- Say "quiet" for a channel with nothing new rather than padding it.
- Read and write files only — never send anything to a channel. Save the
  digest as a dated file in your own folder and notify the owner it is ready.
"""


SUPPORT_PROMPT = """\
You draft replies to support messages. Drafts, never sends.

How to work:
- Read the unread messages in the support channels first, oldest first.
- One draft per message needing a reply: answer the question asked, say
  plainly what you could not answer, and never promise a refund, a fix date
  or anything else only a person can commit to.
- Draft with message_draft and stop there — sending is the owner's decision,
  every time, and this agent never holds the send.
- Reply with the drafts so they can be read without opening anything.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'standup-digest': {
        'name': 'Standup digest',
        'tagline': 'Writes the team\'s morning digest from its channels.',
        'description': (
            'A scheduled agent: every weekday morning it reads the team\'s '
            'message channels, writes a digest of what shipped, what is '
            'blocked and what was decided, and notifies you. Read-only on the '
            'channels — it never sends anything anywhere.'
        ),
        'icon': 'calendar-clock',
        'tags': ['team', 'scheduled', 'messaging'],
        'requirements': [],
        'config': {
            'name': 'Standup digest',
            'brief': STANDUP_PROMPT,
            'temperature': 0.2,
            'tools': {'talk': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'schedule': '0 9 * * 1-5',
            # Required alongside a schedule: without it the sweep's every
            # firing is refused. See `AgentSerializer.validate`.
            'allowUnattended': True,
        },
    },

    'support-drafts': {
        'name': 'Support drafts',
        'tagline': 'Drafts replies to support messages. Never sends.',
        'description': (
            'Reads the unread messages in your support channels and drafts one '
            'reply per message, saying plainly what it could not answer and '
            'never promising what only a person can commit to. Drafts only: '
            'sending stays yours, every time.'
        ),
        'icon': 'inbox',
        'tags': ['team', 'support', 'messaging'],
        'requirements': [],
        'config': {
            'name': 'Support drafts',
            'brief': SUPPORT_PROMPT,
            'temperature': 0.3,
            'tools': {'talk': True},
            'fileAccess': 'none',
            # `ask`: a draft is a proposal and sending is irreversible, so a
            # human stays in the loop on everything leaving the account.
            'autonomy': 'ask',
            'notifyOnHitl': True,
            'spendCapRupees': 300,
        },
    },
}
