"""
Native Google connector tools: Gmail, Drive, Sheets and Calendar over REST.

These replaced four `npx` MCP servers (2026-09-17). Each tool declares
`connector=<icon_slug>` so the matching `MCPServer` card (`type='native'`)
governs it — see `mcp_integration/native.py` for how a card, a credential and a
user's switch decide whether the tools are offered.

  client     the one door to Google: token, retry, error classification
  gmail      mailbox search, read, draft, send, labels, trash
  drive      Drive files (search, read, create) and Sheets values
  calendar   calendars, events, free/busy, invitations
  docs       Docs documents: structural read, create, append
"""
from . import calendar, docs, drive, gmail  # noqa: F401 — imported for registration
