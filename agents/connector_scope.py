"""
Which connector *tools* an agent may reach, not just which connections.

Capability in this runtime is two questions: a grant says *whether*, a scope
says *which*. Files got both (`fileOps` + `sandbox['fileAccess']`), knowledge
bases got both (`rag` + `agent_context['knowledgeBases']`), and connectors got
one and a half — `tool_grants['mcp']` said whether, `agent_context['connectors']`
said which *connection*, and inside a connection it was everything the server
advertised. Choosing a mailbox granted sending and deleting along with reading.

The only thing standing in between was `chat/tools/permissions.py`, which gates
a credentialed call for approval unless its name starts with a read-only verb.
That is a good backstop and a bad boundary: it decides whether a call *pauses*,
never whether the tool should have been in the toolbox. Set `autonomy: full`, or
run on a schedule with nobody watching, and the gate is gone while the send tool
is still there.

**Modes, not a list of names.** A stored allowlist of MCP tool names goes stale
in both directions, because the names are minted at runtime by somebody else's
server: a renamed tool silently narrows the agent, and a newly added one would
silently widen it. So the stored shape is a *mode* per connection, and only the
strictest mode names tools at all:

    all       everything the connection offers — today's behaviour, and the
              default for every agent that already exists
    read      only tools whose names claim they observe, judged per turn by
              `permissions.looks_read_only`. Derived rather than stored, which
              is what makes it survive a catalogue change: a tool added next
              week is judged by the same rule as one added last year
    selected  a named set, intersected with what the server actually advertises.
              A name that has disappeared is dropped; a tool that appears later
              is *not* included, because the user picked a set and additions
              were not in it. Deny-by-default is the only safe direction when
              the catalogue belongs to a third party

**Names are compared normalised.** At dispatch the runtime holds only the
encoded name (`mcp__7__send-email_ab12cd`), whose middle section is the original
run through `_SAFE_TOOL_NAME_RE` — which keeps letters, digits, `_` and `-`, and
replaces everything else. Both sides of every comparison go through the same
`normalise`, so a stored `Send-Email` matches an encoded `send-email` while
`send_email` stays a different name. Folding `-` into `_` here would be a
*widening*: two tools that differ only by separator would become one, and the
user picked one of them.
"""
from __future__ import annotations

from dataclasses import dataclass

#: The whole vocabulary. `all` is the default and the only one that needs no
#: further information; `selected` is the only one that stores names.
MODES = ('all', 'read', 'selected')


def normalise(name: str) -> str:
    """A tool name in the one form both doors can compare.

    The same sanitiser `encode_tool_name` applies, plus case folding: a name is
    stored as the server spells it and arrives at dispatch as the encoded middle
    section, and only running both through this makes them comparable. It is
    deliberately not more aggressive than the encoder — see the module note.
    """
    from mcp_integration.tool_provider import _SAFE_TOOL_NAME_RE

    return _SAFE_TOOL_NAME_RE.sub('_', (name or '').strip().lower()).strip('_')


@dataclass(frozen=True, slots=True)
class ConnectorScope:
    """The resolved answer to "may this agent call this connector tool?"."""

    #: Connection ids this agent may reach at all.
    server_ids: frozenset[int]
    #: id -> mode. A server id missing from this map is `all`, which is what a
    #: bare integer in the stored list means.
    modes: dict[int, str]
    #: id -> normalised tool names, for `selected` only.
    tools: dict[int, frozenset[str]]

    def server_allowed(self, server_id: int) -> bool:
        return server_id in self.server_ids

    def tool_allowed(self, server_id: int, tool_name: str) -> bool:
        """Whether one tool of one connection is in scope.

        `tool_name` may be the original or the encoded middle section; both
        normalise to the same string.
        """
        if server_id not in self.server_ids:
            return False
        mode = self.modes.get(server_id, 'all')
        if mode == 'all':
            return True
        if mode == 'read':
            from chat.tools.permissions import looks_read_only

            return looks_read_only(normalise(tool_name))
        return normalise(tool_name) in self.tools.get(server_id, frozenset())

    def describe(self, server_id: int) -> str:
        """For a refusal the model can act on rather than retry."""
        mode = self.modes.get(server_id, 'all')
        if mode == 'read':
            return 'this agent may only read from that connection'
        if mode == 'selected':
            return 'that tool is not one of the ones this agent was given'
        return 'that connection'


def parse(raw: list | None) -> ConnectorScope | None:
    """`agent_context['connectors']` -> a scope, or None for unrestricted.

    None rather than "every connection" is load-bearing, and for the same reason
    it is in `kb_scope_for`: this selection sat in the builder long before
    anything read it, so enforcement must not silently empty the toolbox of
    every agent that never made a choice. An empty list is therefore
    *unrestricted*, and only a deliberate non-empty one narrows.

    Both stored shapes are accepted. A bare `7` is the pre-2026-09-03 form and
    means mode `all`, so no migration is needed and no existing agent narrows.
    Junk entries are skipped rather than rejected — the field held presentation
    slugs (`'gdrive'`, `'photos'`) two revisions ago, and a row migration `0021`
    did not reach should degrade to the behaviour it has always had.
    """
    if not raw:
        return None

    server_ids: set[int] = set()
    modes: dict[int, str] = {}
    tools: dict[int, frozenset[str]] = {}

    for entry in raw:
        if isinstance(entry, bool):
            continue
        if isinstance(entry, int):
            server_ids.add(entry)
            continue
        if not isinstance(entry, dict):
            continue
        server_id = entry.get('id')
        if not isinstance(server_id, int) or isinstance(server_id, bool):
            continue
        server_ids.add(server_id)
        mode = entry.get('mode') if entry.get('mode') in MODES else 'all'
        # A `selected` entry naming nothing would be a connection with no usable
        # tools, which is not something a user can have meant — it reads as the
        # connection being chosen and the picking not finished, so it stays at
        # `all` until names arrive.
        names = frozenset(
            normalise(t) for t in (entry.get('tools') or []) if isinstance(t, str)
        ) - {''}
        if mode == 'selected' and not names:
            mode = 'all'
        modes[server_id] = mode
        if names:
            tools[server_id] = names

    if not server_ids:
        return None
    return ConnectorScope(frozenset(server_ids), modes, tools)


def for_agent(agent) -> ConnectorScope | None:
    """The scope for one `SubAgent` row."""
    return parse((agent.agent_context or {}).get('connectors'))
