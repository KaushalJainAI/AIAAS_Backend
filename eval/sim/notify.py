"""
Simulated `notify_user`: the owner's notification feed, as a list.

Unlike the other simulators this one has no fixtures and exists in every
world. `notify_user` is always available to agents — an unattended agent's
only way to say "this needs you" — so withholding it would evaluate an agent
missing the tool it relies on, and letting it through would put a real
notification (and a web push) in the owner's feed for every test attempt.
So it is answered here: recorded, capped like the real tool, and shown in
the what-changed panel. Result shapes mirror `chat/tools/workspace.py`.
"""
from __future__ import annotations

import json

TOOLS = ('notify_user',)

#: The real tool's default per-run cap (`MAX_NOTIFICATIONS_PER_RUN`).
MAX_PER_RUN = 3
TITLE_CHARS = 120
MESSAGE_CHARS = 1000


class NotifySim:
    TOOLS = TOOLS

    def __init__(self, fixtures: dict | None = None):
        self.sent: list[dict] = []

    def handles(self, name: str) -> bool:
        return name in TOOLS

    def run(self, name: str, args: dict) -> str:
        title = str((args or {}).get('title') or '').strip()[:TITLE_CHARS]
        message = str((args or {}).get('message') or '').strip()[:MESSAGE_CHARS]
        if not title or not message:
            return json.dumps({'error': 'Both title and message are required.'})
        if len(self.sent) >= MAX_PER_RUN:
            return json.dumps({
                'error': f'This run has already sent {MAX_PER_RUN} notifications, '
                         f'which is the limit. Put the rest in your answer.'})
        self.sent.append({'title': title, 'message': message})
        return json.dumps({
            'sent': True,
            'remaining': MAX_PER_RUN - len(self.sent),
            'rendered': 'The user has been notified. Do not repeat this in every turn.',
        })

    def snapshot(self) -> dict:
        return {'sent': list(self.sent)}

    def changes(self) -> dict:
        return {'notified': list(self.sent[:20])} if self.sent else {}

    def apply_expected(self, expect: dict) -> None:
        """Nothing to perform: no grader checks notifications yet."""


__all__ = ['TOOLS', 'NotifySim']
