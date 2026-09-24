"""
The `paperwork` pack: signatures tracked until they come back, and
recordings turned into minutes. Signatures need `ESIGN_ENGINE`.

Each entry is a flat `AgentConfig` plus the card text shown on Explore.
See `agents/gallery/__init__.py` for the rules every template follows.
"""
from __future__ import annotations

from typing import Any

#: The templates this pack installs, in install order.
PACK: list[str] = ['esign-agent', 'meeting-minutes']


ESIGN_PROMPT = """\
You send documents out for e-signature and track them home.

How to work:
- Read the document first and say who signs where before anything is sent.
  A signature request with the wrong signer or the wrong file wastes
  everyone's time and cannot be unsent.
- Send with request_signature only after a human has approved that exact
  file and that exact signer list.
- Track with signature_status and report plainly: who signed, who has not,
  and what is overdue. Save the signed file where it belongs when it lands.
"""


MINUTES_PROMPT = """\
You turn recordings into minutes people can act on.

How to work:
- Transcribe the recording first — the whole of it, before deciding what
  matters. A summary written from the first five minutes is a summary of
  the first five minutes.
- Minutes are decisions, owners and dates. Discussion goes in only where it
  explains a decision; who said what about the weather does not.
- Mark anything you could not hear rather than inventing it.
- Save as a document with render_document in your own folder and reply with
  the path and the decision list.
"""


TEMPLATES: dict[str, dict[str, Any]] = {

    'esign-agent': {
        'name': 'Signature sender',
        'tagline': 'Sends documents for e-signature and tracks them home.',
        'description': (
            'Reads the document, confirms the signer list, and sends it for '
            'signature only after you approve that exact file and those exact '
            'signers. Tracks who signed and what is overdue. Reads your files '
            'and writes only inside its own folder.'
        ),
        'icon': 'pen',
        'tags': ['esign', 'paperwork'],
        'requirements': [],
        'config': {
            'name': 'Signature sender',
            'brief': ESIGN_PROMPT,
            'temperature': 0.2,
            'tools': {'esign': True, 'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            # `ask`: a sent signature request cannot be unsent.
            'autonomy': 'ask',
            'notifyOnHitl': True,
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },

    'meeting-minutes': {
        'name': 'Meeting minutes',
        'tagline': 'Turns a recording into decisions, owners and dates.',
        'description': (
            'Transcribes the whole recording before deciding what matters, '
            'then writes minutes as a document: decisions, owners and dates, '
            'with anything unheard marked rather than invented. Reads your '
            'files and writes only inside its own folder.'
        ),
        'icon': 'book-open',
        'tags': ['voice', 'meetings'],
        'requirements': [],
        'config': {
            'name': 'Meeting minutes',
            'brief': MINUTES_PROMPT,
            'temperature': 0.3,
            'tools': {'voice': True, 'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
            'outputContract': 'files',
        },
    },
}
