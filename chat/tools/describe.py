"""
A tool call, as a person reads it.

Four surfaces ask a user to approve a tool call — the chat card, the Inbox, a
notification row, and a device push — and until this module existed each one
improvised from the same two raw values. The results were what you would
expect. The Inbox said `Approve mcp__7__send_email_ab12cd34?` and its message
named no arguments at all, because `_approval_requested` never passed them on.
The chat card printed `JSON.stringify(args, null, 2)`. The notification list
printed the whole `data` payload, thread ids included, under every row.

None of that is four bugs. It is one missing layer: nothing ever turned a call
into a sentence, so every renderer had to, and a renderer holding a
`mcp__7__send_email_ab12cd34` and a dict of unknown shape can only print them.

## What it promises

`describe_call` is **synchronous and does no I/O**. That is a requirement, not
a convenience: `mcp_reads_only` next door is synchronous for the same reason,
because the dispatch planner runs per call per turn and must not grow a
database round trip. A connection's *display name* does need a row, so it is
resolved by `describe_call_async` at the two places that pause a run — both
already async, both already waiting on a human — and passed in. A renderer
never looks anything up.

## What it refuses to do

**It never renders markup.** Arguments are third-party data: an MCP server's
response, a model's generated text, a filename someone chose. The Inbox passes
`message` through `MarkdownMessage`, so a `sentence` built by pasting an
argument in would let a tool call's contents style the approval screen it is
asking to get past. Values are truncated, and `fields` are label/value pairs
the renderer places itself.

**It redacts rather than trusts.** A tool that takes an API key as an argument
would otherwise print it into a notification row that lives in the database
for ever. The check is on the key's *name* and is deliberately eager: a false
positive costs a reader one field they can still see in the raw disclosure, a
false negative writes a live credential into a list nobody thinks of as
sensitive.

**It shows a shape, not a payload.** A 4,000-word email body is not more
informative than "1,180 words"; it is less, because it pushes the recipient
off the screen. Long values are described rather than quoted.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: How many argument rows a person will actually read before clicking. The cap
#: exists because MCP schemas routinely carry a dozen optional parameters, and
#: an approval screen that scrolls is one that gets approved unread.
MAX_FIELDS = 6

#: Characters kept of any single value. Past this the value is summarised.
MAX_VALUE_CHARS = 160

#: Beyond this a value is described by size rather than shown at all.
LONG_VALUE_CHARS = 400

#: Argument names whose values never reach a screen. Matched as substrings,
#: lowercased, because the shape varies per server: `apiKey`, `api_key`,
#: `access_token`, `client_secret`, `Authorization` are all the same field.
_SECRET_HINTS = (
    'password', 'passwd', 'secret', 'token', 'api_key', 'apikey', 'api-key',
    'credential', 'authorization', 'auth', 'private_key', 'privatekey',
    'session_key', 'cookie', 'signature',
)

#: Argument names worth showing first when a call has more than `MAX_FIELDS`.
#: Ordered: the recipient of an action matters more than its formatting.
_SALIENT_FIRST = (
    'to', 'recipient', 'recipients', 'cc', 'bcc', 'email', 'address',
    'path', 'file', 'filename', 'file_path', 'url', 'channel', 'repo',
    'subject', 'title', 'name', 'query', 'q', 'command', 'sql',
    'id', 'message_id', 'thread_id', 'calendar_id',
)

#: Built-in tools whose generated wording would read badly. Only the ones a
#: user is actually asked about are worth a hand-written phrase; everything
#: else humanises well enough from its own name.
_BUILTIN_PHRASES = {
    'write_file': 'Save a file',
    'edit_file': 'Edit a file',
    'delete_file': 'Delete a file',
    'make_directory': 'Create a folder',
    'move_file': 'Move or rename a file',
    'copy_file': 'Copy a file',
    'run_agent': 'Hand this work to another agent',
    'invoke_subagent': 'Hand this work to another agent',
    'create_agent': 'Create a new agent',
    'update_agent': 'Change an agent',
    'list_eval_suites': 'List eval suites',
    'get_scorecard': 'Show an agent’s 0–100 scorecard',
    'create_eval_suite_from_starter': 'Create an eval suite from a starter',
    'create_eval_suite': 'Create an empty eval suite',
    'add_eval_case': 'Draft an eval case',
    'generate_eval_world': 'Build an eval world and its cases (spends judge credits)',
    'import_eval_cases_from_runs': 'Draft eval cases from real runs',
    'get_eval_results': 'Show one eval sweep’s results',
    'run_eval_suite': 'Run an eval suite (spends credits)',
    'execute_python': 'Run Python code',
    'generate_image': 'Generate an image (this costs money)',
    'download_file': 'Download a file from the web',
    'edit_deck': 'Change a presentation',
    'edit_document': 'Change a Word file',
    'edit_workbook': 'Change a spreadsheet',
    'export_file': 'Save a file in another format',
    'extract_data': 'Extract fields from documents',
    'file_versions': 'List the earlier versions of a file',
    'notify_user': 'Send you a notification',
    'read_workbook': 'Read a spreadsheet',
    'render_pdf': 'Create a PDF',
    'restore_file_version': 'Restore an earlier version of a file',
    'render_diagram': 'Draw a diagram',
    'send_email': 'Send an email',
    'remember_about_user': 'Remember something about you',
    'forget_about_user': 'Forget something about you',
}

_WORD_RE = re.compile(r'[^\W\d_]+', re.UNICODE)


def _humanise(name: str) -> str:
    """`send_email` / `sendEmail` / `send-email` -> `Send email`."""
    spaced = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name or '')
    words = [w for w in re.split(r'[\s_\-.]+', spaced) if w]
    if not words:
        return 'this tool'
    first, *rest = words
    return ' '.join([first.capitalize(), *(w.lower() for w in rest)])


def _is_secret(key: str) -> bool:
    lowered = str(key).lower()
    return any(hint in lowered for hint in _SECRET_HINTS)


def _describe_value(value: Any) -> str:
    """One argument, as a short string that is safe to place in a layout."""
    if value is None:
        return '—'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (list, tuple)):
        if not value:
            return 'none'
        rendered = ', '.join(_describe_value(v) for v in value[:4])
        if len(value) > 4:
            rendered = f'{rendered}, and {len(value) - 4} more'
        return rendered[:MAX_VALUE_CHARS]
    if isinstance(value, Mapping):
        # A nested object is structure, not content. Naming its keys says more
        # about what is being sent than the first 160 characters of its JSON.
        keys = ', '.join(str(k) for k in list(value)[:5])
        return f'{{{keys}}}' if keys else '{}'

    text = str(value).strip()
    if not text:
        return '—'
    # Newlines would break out of a single-line field and, in a markdown
    # renderer, out of the paragraph entirely.
    text = ' '.join(text.split())
    if len(text) > LONG_VALUE_CHARS:
        return f'{len(_WORD_RE.findall(text))} words'
    if len(text) > MAX_VALUE_CHARS:
        return f'{text[:MAX_VALUE_CHARS - 1]}…'
    return text


def _fields(args: Mapping[str, Any] | None) -> list[dict[str, str]]:
    """The arguments worth reading, most identifying first."""
    if not isinstance(args, Mapping):
        return []

    def rank(key: str) -> tuple[int, int]:
        lowered = str(key).lower()
        for i, salient in enumerate(_SALIENT_FIRST):
            if lowered == salient:
                return (0, i)
        for i, salient in enumerate(_SALIENT_FIRST):
            if salient in lowered:
                return (1, i)
        return (2, 0)

    out: list[dict[str, str]] = []
    for key in sorted(args, key=rank):
        if len(out) >= MAX_FIELDS:
            break
        value = args[key]
        # An absent optional parameter is noise on an approval screen.
        if value is None or value == '' or value == [] or value == {}:
            continue
        out.append({
            'label': _humanise(str(key)),
            'value': '••••••••' if _is_secret(key) else _describe_value(value),
        })
    return out


def _world_cost_note(args: Mapping[str, Any] | None) -> str:
    """What `generate_eval_world` is about to spend, on its approval card.

    A fixed estimate, not a quote: facts, world, cases, solve and verify are
    one judge call each, so the count barely moves with the case count —
    only the size of two of the calls does.
    """
    try:
        n = max(1, min(int((args or {}).get('cases') or 12), 25))
    except (TypeError, ValueError):
        n = 12
    return (f'About 5 judge-model calls for up to {n} cases, billed to the '
            "user's key. Everything arrives as drafts.")


#: Tools whose approval card carries a computed note under the sentence.
_APPROVAL_NOTES = {
    'generate_eval_world': _world_cost_note,
}


def _approval_note(name: str, args: Mapping[str, Any] | None) -> str:
    try:
        fn = _APPROVAL_NOTES.get(name or '')
        return fn(args) if fn else ''
    except Exception:  # noqa: BLE001 - a note must not break the card
        return ''


def describe_call(
    name: str, args: Mapping[str, Any] | None = None, *, server: str = '',
    label: str = '',
) -> dict[str, Any]:
    """
    Render one tool call for a human.

    `server` is the connection's display name, which only a caller that can
    afford a database read may supply — see `describe_call_async`. Without it
    the description still works and simply does not name the connection, which
    is the right degradation: a description that fails because a row could not
    be read would take the approval screen down with it.

    `label` names the worker asking ("Implementer #2 (task t3: add retry)"),
    passed in by the caller — this function does no lookup of its own. Empty
    for every non-worker call, where the sentence reads exactly as before.

    Never raises. Every caller is on a path where a run has already stopped and
    asked, and a formatting error must not be what fails it.
    """
    raw_name = name or ''
    try:
        from mcp_integration.tool_provider import decode_tool_name, is_mcp_tool

        decoded = decode_tool_name(raw_name) if is_mcp_tool(raw_name) else None
    except Exception:  # noqa: BLE001
        # Only reachable if `mcp_integration` cannot import at all. Logged
        # rather than swallowed: the fallback describes an MCP call as though
        # it were a built-in, which is wrong in a way nobody would notice.
        logger.warning('[Describe] MCP name decoding unavailable for %s', raw_name)
        decoded = None

    if decoded is not None:
        from .permissions import strip_encoded_digest
        tool = strip_encoded_digest(decoded[1])
        phrase = _humanise(tool)
        title = f'{phrase} · {server}' if server else phrase
        where = f'your {server} connection' if server else 'a connected account'
        sentence = f'{phrase} using {where}.'
    else:
        tool = raw_name
        phrase = _BUILTIN_PHRASES.get(raw_name) or _humanise(raw_name)
        title = phrase
        sentence = f'{phrase}.'

    if (label or '').strip():
        who = label.strip()
        title = f'{who} · {title}'
        first = sentence[:1].lower() + sentence[1:] if sentence else sentence
        sentence = f'{who} wants to {first}'

    note = _approval_note(raw_name, args)
    if note:
        sentence = f'{sentence} {note}'

    return {
        'title': title,
        'sentence': sentence,
        'server': server,
        'tool': tool,
        'fields': _fields(args),
    }


async def _describe_worker_request(args: Mapping[str, Any]) -> dict[str, Any] | None:
    """The card for a manager's `answer_subagent`: the *worker's* request.

    What the person is deciding is whether the worker may act, so the card
    shows the worker's own call as it was rendered when it paused — never the
    manager's tool name and a pair of ids.
    """
    try:
        # The card is built for the call being paused on, which is the
        # person's own; ownership is re-checked by the tool at dispatch.
        from agents.models import HITLRequest

        row = await (HITLRequest.objects
                     .filter(execution__execution_id=str(args.get('execution_id') or ''),
                             node_id=str(args.get('call_id') or ''), status='pending')
                     .select_related('execution__subagent').afirst())
    except Exception:  # noqa: BLE001 — fall back to the generic card
        return None
    if row is None:
        return None
    ctx = row.context_data or {}
    worker = ctx.get('detail') or {}
    agent = getattr(row.execution.subagent, 'name', '') or 'The agent'
    sentence = worker.get('sentence') or row.message or 'Take an action.'
    if ' wants to ' not in sentence:
        # A labelled worker's sentence already names who is asking.
        sentence = f'{agent} wants to {sentence[:1].lower() + sentence[1:]}'
    return {
        'title': f"{agent} · {worker.get('title') or 'wants to act'}",
        'sentence': f'{sentence} Allow it?',
        'server': worker.get('server', ''),
        'tool': str(ctx.get('tool') or 'answer_subagent'),
        'fields': list(worker.get('fields') or []),
    }


async def describe_call_async(
    name: str, args: Mapping[str, Any] | None = None, *, label: str = '',
) -> dict[str, Any]:
    """
    `describe_call`, plus the connection's display name.

    Only for callers that are already paused and already async — opening a
    `HITLRequest`, or emitting the frame that stops a chat turn. The lookup is
    one indexed read against a run that is waiting on a human, so it is free
    where it is used and would be a per-call cost anywhere else.
    """
    if name == 'answer_subagent':
        described = await _describe_worker_request(args or {})
        if described is not None:
            return described

    server = ''
    try:
        from .permissions import _server_for

        row = await _server_for(name)
        if row is not None:
            server = row.name or ''
    except Exception:  # noqa: BLE001
        logger.warning('[Describe] Could not name the connection behind %s', name)

    return describe_call(name, args, server=server, label=label)
