"""
The builder's chat: a plain-language description turned into knob changes.

The premise the agent model rests on is that an agent is a *configuration*, so
something that can write a configuration can build an agent. Until this existed
the builder's chat pane was a keyword matcher in the browser
(`src/lib/agentProposals.ts`): "hi" moved nothing, and so did every description
whose words happened to miss the table — the user was told it "couldn't tell
which knobs that should move" about a brief that named its source, its job and
its cadence in the field directly to the right.

Three things this endpoint has to get right, and they are why it is a view
rather than a prompt in the frontend.

**The model is given the account's real catalogue.** Connections, knowledge
bases and skills are *ids*, and an id only exists on the server. A model asked
to "enable Gmail" with no catalogue can only invent a number, and an invented
number is a 400 on the next save.

**Nothing the model returns is trusted.** `KNOBS` is the single table of what
may move and what each one accepts, and it is used twice — once to write the
part of the prompt that describes the knob, once to check the value that comes
back. One table, so the description a model is given cannot drift from the
validation it is held to. Anything unknown, out of range, or naming an id the
caller cannot see is dropped rather than corrected: a silently corrected value
is a knob the user never chose.

**A proposal is not a save.** The response is a list of changes for the board to
highlight; the user still presses Save, and `AgentSerializer` validates it again
there. The final pass here runs the *merged* config through that same serializer
so a proposal cannot be one the user is then unable to save — the cross-field
rules (a schedule needs `allowUnattended`, shell excludes open egress) are
checked in exactly one place, and it is not this file.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable

from adrf.decorators import api_view as async_api_view
from asgiref.sync import sync_to_async
from django.conf import settings as django_settings
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from agents.triggers import (
    describe as describe_cron,
    is_valid as cron_is_valid,
    zone_is_valid,
)
from agents.views.agents import (
    AUTONOMY,
    FILE_ACCESS,
    MAX_TAGS,
    OUTPUT_CONTRACTS,
    SETTABLE_STATUS,
    AgentSerializer,
)
from agents.agent.orchestrator import MAX_PARALLEL_WORKERS
from llm.effort import LADDER as EFFORT_LADDER
from workflow_backend.thresholds import MAX_RUN_SECONDS, MIN_RUN_SECONDS

logger = logging.getLogger(__name__)

#: How much of the builder conversation is replayed to the model. A builder
#: chat is a handful of short turns; sending all of it unbounded would let one
#: long session make every later turn slower and dearer for no gain.
MAX_HISTORY_TURNS = 8
MAX_MESSAGE_CHARS = 4000


class ConfigureSerializer(serializers.Serializer):
    """One turn of the builder chat."""

    message = serializers.CharField(max_length=MAX_MESSAGE_CHARS)
    #: The board as it stands. Optional: a brand-new agent has nothing saved
    #: yet, and the proposal is against what the user is looking at, not
    #: against what is in the database.
    config = serializers.DictField(required=False, default=dict)
    #: Prior turns, oldest first, as `{role: 'user'|'agent', text}`.
    history = serializers.ListField(
        child=serializers.DictField(), required=False, default=list
    )
    #: The saved agent this conversation is about, if there is one. When set,
    #: the turn is kept (see `_remember_turn`) so the builder chat survives a
    #: reload. A brand-new board has no id and nothing is stored for it.
    agent_id = serializers.IntegerField(required=False, allow_null=True, default=None)

    def validate_message(self, value):
        text = value.strip()
        if not text:
            raise serializers.ValidationError('Say what the agent should do.')
        return text


# --------------------------------------------------------------------------
# The knob table: what may move, what it accepts, and how it is described.
# --------------------------------------------------------------------------

class Reject(ValueError):
    """A proposed value this knob cannot take."""


@dataclass(frozen=True)
class Knob:
    """One settable path, described once and validated once."""

    #: UI copy for the change chip. Ours, never the model's — a label that
    #: varied per proposal would make the board unreadable.
    label: str
    #: What the model is told about this knob, values included.
    describe: str
    #: Value -> value, raising `Reject`. Second argument is the catalogue.
    coerce: Callable[[Any, 'Catalogue'], Any]


def _bool(value, _cat):
    if isinstance(value, bool):
        return value
    raise Reject('expected true or false')


def _text(limit):
    def coerce(value, _cat):
        if not isinstance(value, str):
            raise Reject('expected a string')
        text = value.strip()
        if not text or len(text) > limit:
            raise Reject(f'expected 1-{limit} characters')
        return text
    return coerce


def _one_of(allowed):
    def coerce(value, _cat):
        if value not in allowed:
            raise Reject(f'expected one of {sorted(allowed)}')
        return value
    return coerce


def _number(low, high, cast=int):
    def coerce(value, _cat):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Reject('expected a number')
        number = cast(value)
        if not low <= number <= high:
            raise Reject(f'expected {low}-{high}')
        return number
    return coerce


def _ids(field):
    """Ids checked against what this caller can actually see.

    The whole change is dropped rather than the offending id: a partial
    selection is a different selection, and an agent pointed at three of the
    four sources it was asked for answers confidently from the wrong corpus.
    """
    def coerce(value, cat):
        if not isinstance(value, list) or any(
            isinstance(i, bool) or not isinstance(i, int) for i in value
        ):
            raise Reject('expected a list of ids')
        known = {row['id'] for row in getattr(cat, field)}
        unknown = sorted(set(value) - known)
        if unknown:
            raise Reject(f'no such id: {unknown}')
        return sorted(set(value))
    return coerce


def _tags(value, _cat):
    """A short list of short labels, trimmed and de-duplicated.

    Refused rather than trimmed when it is too long, for the same reason the
    delegation payload is: the author is a model that can be told to shorten it.
    """
    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        raise Reject('expected a list of strings')
    if len(value) > MAX_TAGS:
        raise Reject(f'expected at most {MAX_TAGS} tags')
    tags, seen = [], set()
    for raw in value:
        tag = ' '.join(raw.split())
        if not tag or len(tag) > 30:
            raise Reject('each tag is 1-30 characters')
        if tag.lower() not in seen:
            seen.add(tag.lower())
            tags.append(tag)
    return tags


def _connectors(value, cat):
    """A connection selection, in either stored shape.

    `read` is offered to the model but `selected` is not: naming individual
    tools needs the live catalogue of a third-party server, which this endpoint
    does not fetch, so a model doing it would be guessing names. The mode that
    is *derived* from the name at run time is the one that is safe to propose.
    """
    if not isinstance(value, list):
        raise Reject('expected a list of connections')
    known = {row['id'] for row in cat.connectors}
    out = []
    for entry in value:
        if isinstance(entry, bool):
            raise Reject('expected a list of connections')
        if isinstance(entry, int):
            server_id, mode = entry, 'all'
        elif isinstance(entry, dict):
            server_id, mode = entry.get('id'), entry.get('mode', 'all')
        else:
            raise Reject('each connection is an id or {id, mode}')
        if not isinstance(server_id, int) or isinstance(server_id, bool):
            raise Reject('each connection needs an integer id')
        if server_id not in known:
            raise Reject(f'no such connection: {server_id}')
        if mode not in ('all', 'read'):
            raise Reject("mode is 'all' or 'read'")
        out.append(server_id if mode == 'all' else {'id': server_id, 'mode': 'read'})
    return out


def _cron(value, _cat):
    if not isinstance(value, str):
        raise Reject('expected a cron string')
    cron = value.strip()
    # Blank is a real answer: it is how a schedule is taken off an agent.
    if not cron:
        return ''
    if not cron_is_valid(cron):
        raise Reject('expected five cron fields, e.g. "0 9 * * 1"')
    return cron


def _timezone(value, _cat):
    if not isinstance(value, str) or not zone_is_valid(value.strip()):
        raise Reject('expected an IANA zone name, e.g. "Asia/Kolkata"')
    return value.strip()


def _globs(value, _cat):
    """A list of project-relative globs, e.g. ["src/api/**"]."""
    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        raise Reject('expected a list of globs, e.g. ["src/api/**"]')
    out = []
    for raw in value:
        pattern = str(raw or '').strip().lstrip('/')
        if not pattern:
            raise Reject('each glob is a path like "src/api/**", not blank')
        if len(pattern) > 300:
            raise Reject('each glob is at most 300 characters')
        out.append(pattern)
    return sorted(set(out))


def _command_classes(value, _cat):
    """Command classes for `commandScope`: test | lint | build | run | install | any."""
    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        raise Reject('expected a list like ["test", "lint"]')
    from agents.agent.runtime import CODE_COMMAND_CLASSES

    cleaned = sorted({str(c or '').strip().lower() for c in value if str(c or '').strip()})
    unknown = sorted(set(cleaned) - set(CODE_COMMAND_CLASSES))
    if unknown:
        raise Reject(f'unknown classes: {unknown}. Allowed: {list(CODE_COMMAND_CLASSES)}.')
    return cleaned


def _playbooks(value, _cat):
    """Playbook slugs shipped as code under `agents/playbooks/code/`."""
    if not isinstance(value, list) or any(not isinstance(t, str) for t in value):
        raise Reject('expected a list of playbook slugs')
    from agents.playbooks import PLAYBOOK_SLUGS

    cleaned = [str(s or '').strip() for s in value if str(s or '').strip()]
    unknown = sorted({s for s in cleaned if s not in PLAYBOOK_SLUGS})
    if unknown:
        raise Reject(f'unknown playbooks: {unknown}. Allowed: {sorted(PLAYBOOK_SLUGS)}.')
    seen: set[str] = set()
    out: list[str] = []
    for slug in cleaned:
        if slug not in seen:
            seen.add(slug)
            out.append(slug)
    return out


#: One line per grant the runtime actually serves — `TOOL_KEYS` minus
#: `runtime.UNSERVED_GRANTS`, asserted in the tests, so a tool added to the
#: runtime cannot quietly go undescribed to the model that hands it out.
#: Empty since P6 served `shell` through the workspace code tools.
TOOL_HELP = {
    'codeExecution': 'run Python in the sandbox — arithmetic, CSV work, anything computed',
    'webSearch': 'search the web',
    'scrape': 'fetch and read a web page it has the URL for',
    'fileOps': "read and write the user's own files",
    'office': (
        'create PowerPoint decks, Excel workbooks and Word documents in its '
        'folder. Needs `fileAccess` other than none'
    ),
    'media': (
        'generate images, billed to the user\'s OpenRouter account and saved '
        'into its folder. Needs `fileAccess` other than none'
    ),
    'browser': (
        'use a real browser: read JavaScript pages, and click/type on the sites '
        'listed in `browserDomains` (empty: read only). Sessions keep logins '
        'per site; vault logins in `browserLogins` are typed, never shown. '
        'Off unless the platform has a browser configured'
    ),
    'publish': (
        'publish hosted pages shareable by link (report, HTML or file). '
        'Outward-facing: above link visibility it pauses for a human, and an '
        'unattended run may only publish link pages'
    ),
    'rag': "search the user's knowledge bases (and read scanned documents)",
    'voice': (
        'transcribe recordings the user has, and speak text into audio files. '
        'Needs `fileAccess` other than none. Off unless the platform has '
        'speech engines configured'
    ),
    'esign': (
        'send documents out for e-signature and check whether they came back. '
        'Outward-facing: sending pauses for a human. Needs `fileAccess` other '
        'than none. Off unless the platform has an e-sign provider configured'
    ),
    'talk': (
        'message on Slack, WhatsApp, Teams and SMS: read, draft and send. '
        'Sending pauses for a human; unattended runs may only reach '
        '`recipients`'
    ),
    'data': (
        'query the user\'s databases and write where allowed. '
        '`dataConnections` says which'
    ),
    'api': (
        'call the user\'s HTTP APIs. `apiConnections` says which, each in '
        'read or full mode'
    ),
    'compute': (
        'run commands and long jobs on your workspace machine. '
        '`workspaceEgress` says which extra hosts it may reach'
    ),
    'shell': (
        'edit code in its projects: read, write, test, commit, open a PR. '
        '`codeProjects` says which projects'
    ),
    'mcp': (
        'reach connected accounts (Gmail, Drive, Slack, …). Needed for any '
        'connector to work; pair it with `connectors`'
    ),
    'subAgents': 'delegate parts of the job to other agents the user owns',
}

KNOBS: dict[str, Knob] = {
    'name': Knob(
        'Name', 'A short human name for the agent, e.g. "Invoice triage".',
        _text(200),
    ),
    'description': Knob(
        'Description',
        'One line saying what this agent is for, written for someone choosing '
        'between agents — this is what another agent reads when deciding which '
        'one to delegate a job to. Not the instructions; that is `brief`.',
        _text(2000),
    ),
    'tags': Knob(
        'Tags',
        f'Up to {MAX_TAGS} short labels for grouping and search, e.g. '
        '["finance", "weekly"]. Lowercase, one or two words each.',
        _tags,
    ),
    'status': Knob(
        'Status',
        f'{sorted(SETTABLE_STATUS - {"archived"})}. `paused` stops schedules and '
        'delegation without deleting anything. Only set this when the user asks '
        'to pause or resume an agent.',
        # Archiving is filing an agent away, done from the list, not something
        # a description of a job should ever imply.
        _one_of(SETTABLE_STATUS - {'archived'}),
    ),
    'outputContract': Knob(
        'Result shape',
        f'What the answer must come back as: {sorted(OUTPUT_CONTRACTS)}, or "" '
        'for prose. `research` returns text plus the queries run and the '
        'sources used; `extraction` returns rows plus field names; `files` '
        'returns a summary plus the workspace paths written. Set one only '
        'when the user wants a structured result something else will read.',
        _one_of(OUTPUT_CONTRACTS | {''}),
    ),
    'fanoutParallel': Knob(
        'Fan-out width',
        f'1-{MAX_PARALLEL_WORKERS}. How many pieces of a delegated list this '
        'agent works on at once when a parent hands it several. Blank means one '
        'at a time. Only meaningful for an agent other agents delegate to.',
        _number(1, MAX_PARALLEL_WORKERS),
    ),
    'brief': Knob(
        'Brief',
        "The agent's own instructions, written in the second person ('You read "
        "the inbox each morning and…'). This is the system prompt the run is "
        'given, so it should say what to do, what to produce, and what to leave '
        'alone.',
        _text(8000),
    ),
    'temperature': Knob(
        'Temperature',
        '0 to 2. Extraction, classification and anything that must be exact '
        'want 0-0.2; drafting and tone want 0.6-0.8.',
        _number(0, 2, float),
    ),
    'effort': Knob(
        'Reasoning effort',
        "How hard the model thinks before answering: ''(model default), "
        "'none', 'minimal', 'low', 'medium', 'high'. Raise it for multi-step "
        'analysis and anything the agent has to reason its way through; leave '
        'it low or blank for extraction, routing and formatting, where the '
        'extra thinking is billed and changes nothing. A model with no effort '
        'control ignores this.',
        _one_of({'', *EFFORT_LADDER}),
    ),
    'fileAccess': Knob(
        'File access',
        "How much of the user's file tree it may touch: "
        f'{sorted(FILE_ACCESS)}. `scoped` is its own folder only; '
        '`read_all_write_own` reads everything and writes only its own folder, '
        "and is usually right for work that reads the user's documents.",
        _one_of(FILE_ACCESS),
    ),
    **{
        f'tools.{key}': Knob(f'Tool: {key}', f'Grant `{key}` — {help_}.', _bool)
        for key, help_ in TOOL_HELP.items()
    },
    'connectors': Knob(
        'Connections',
        'Which connected accounts this agent may reach, and how much of each. '
        'A list whose entries are either a bare id from the catalogue below, or '
        '{"id": <id>, "mode": "read"} to give it only the read-only tools of '
        'that connection. Prefer `read` for anything that only needs to look — '
        'choosing a mailbox otherwise grants sending and deleting too. Only '
        'meaningful with the `mcp` grant; an empty list means every connection '
        'the user has.',
        _connectors,
    ),
    'delegatesTo': Knob(
        'Delegates to',
        'Which of the user\'s other agents this one may hand work to, by id '
        'from the AGENTS catalogue below. Only meaningful with the `subAgents` '
        'grant. An empty list means any of them, so name the ones the job needs '
        '— an agent that can delegate to an agent with wider grants has those '
        'grants by proxy.',
        _ids('agents'),
    ),
    'knowledgeBases': Knob(
        'Knowledge bases',
        'Which knowledge bases it may search, by id, from the catalogue below. '
        'Needs the `rag` grant.',
        _ids('knowledge_bases'),
    ),
    'skills': Knob(
        'Skills', 'Skill ids from the catalogue below.', _ids('skills'),
    ),
    'useEnvironment': Knob(
        'Environment',
        'Tell it the current time and place. Needed by anything that reasons '
        'about "today", business hours, or a timezone.',
        _bool,
    ),
    'writePaths': Knob(
        'Write paths',
        'Glob list (relative to the project root) this coding agent may '
        'write, e.g. ["src/api/**"]. Empty means unrestricted. Intersected '
        'with the task\'s claims at dispatch, so an implementer writes only '
        'what its task claimed even when its template allows more.',
        _globs,
    ),
    'commandScope': Knob(
        'Command scope',
        'Which `ws_run` classes this coding agent may reach: test, lint, '
        'build, run, install, any. Empty means unrestricted. A literal must '
        'match the project\'s configured command for an allowed class.',
        _command_classes,
    ),
    'playbooks': Knob(
        'Playbooks',
        'Static playbooks appended to the system prompt, shipped as code '
        '(small-diffs, run-tests-before-claiming-done, read-before-edit, '
        'python-testing, ts-react, git-hygiene).',
        _playbooks,
    ),
    'schedule': Knob(
        'Schedule',
        'Five-field cron, e.g. "0 9 * * 1-5" for weekday mornings. Set it only '
        'if the user described a cadence. Setting it also requires '
        '`allowUnattended: true`, or every firing is refused. A schedule is '
        'what makes an agent a standing job; there is no separate mode to set.',
        _cron,
    ),
    'scheduleTimezone': Knob(
        'Timezone',
        'IANA zone the cron is read in, e.g. "Asia/Kolkata". Set it when you '
        'set a schedule and the user named a place.',
        _timezone,
    ),
    'allowUnattended': Knob(
        'Unattended',
        'Whether anything other than the user may start a run — a schedule, a '
        'webhook, a parent agent. Off unless the agent is scheduled or the user '
        'asked for it.',
        _bool,
    ),
    'autonomy': Knob(
        'Autonomy',
        'The approval ladder, strictest first: `plan` (look and report, no tool '
        'that changes anything), `review` (approve every step), `ask` (pauses '
        'before anything leaves the account — the default, and the right answer '
        'for anything that sends, posts or pays), `auto` (runs undoable changes, '
        'still stops before permanent ones), `full` (no gate at all; only when '
        'the user explicitly asks to be left out).',
        _one_of(AUTONOMY),
    ),
    'notifyOnHitl': Knob(
        'Notify on pause', 'Ping the user when it needs an answer.', _bool,
    ),
    # `reviewAgent` is not offered: nothing reads it. It told the user a second
    # agent would check the answers, and none ever did.
    'spendCapRupees': Knob(
        'Spend cap', 'Monthly cap in rupees, 0-100000.', _number(0, 100_000),
    ),
    'maxRunSeconds': Knob(
        'Time limit',
        f'Wall-clock ceiling on one run, in seconds ({MIN_RUN_SECONDS}-'
        f'{MAX_RUN_SECONDS}). Bulk work needs more; a quick lookup needs less.',
        _number(MIN_RUN_SECONDS, MAX_RUN_SECONDS),
    ),
    'recursiveContext': Knob(
        'Recursive context',
        'Fold the oldest steps into a running note when the window fills. '
        'Leave on for long runs.',
        _bool,
    ),
    'compaction': Knob(
        'Compaction',
        'Replace old tool results with a record of the call once they are spent.',
        _bool,
    ),
    'indexing': Knob(
        'Indexing',
        'Archive everything curated away so the agent can fetch it back.',
        _bool,
    ),
}

# Deliberately absent: `provider`, `model`, `summaryModel`, `summaryProvider`.
# A model id is a routing key, not a preference — the catalogue lives in
# `AIModel` and a guessed name is a run that dies at its first call. The picker
# for those is on the board, two inches from the chat.


# --------------------------------------------------------------------------
# The catalogue the model is allowed to name
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Catalogue:
    """What this account actually has, as ids a proposal may name."""

    connectors: list[dict]
    knowledge_bases: list[dict]
    skills: list[dict]
    #: The user's other agents, for `delegatesTo`. Same argument as connectors:
    #: an id only exists on the server, so a model without the list can only
    #: invent one, and an invented id is a 400 on the next save.
    agents: list[dict] = field(default_factory=list)


@sync_to_async
def _catalogue(user) -> Catalogue:
    from agents.models import SubAgent
    from inference.models import visible_knowledge_bases
    from mcp_integration.client import _visible_servers_queryset
    from skills.models import Skill

    connectors = [
        {
            'id': server.id,
            'label': server.label,
            'slug': server.icon_slug or '',
            'about': (server.tagline or '')[:120],
        }
        # `enabled_only=True`, the same predicate the runtime resolves the
        # toolbox through: a connection the user switched off on Connections is
        # not one to propose, because the run would drop it anyway.
        for server in _visible_servers_queryset(user.id, enabled_only=True)[:60]
    ]
    knowledge_bases = [
        {'id': kb.id, 'label': kb.name, 'backend': kb.backend}
        for kb in visible_knowledge_bases(user).only(
            'id', 'name', 'backend')[:50]
    ]
    skills = [
        {'id': skill.id, 'label': skill.title,
         'about': (skill.description or '')[:120]}
        for skill in Skill.objects.filter(user=user).only(
            'id', 'title', 'description')[:50]
    ]
    agents = [
        {'id': row.id, 'label': row.name,
         'about': (row.description or '')[:120]}
        for row in SubAgent.objects.filter(user=user)
        .exclude(status='archived')
        .only('id', 'name', 'description')[:50]
    ]
    return Catalogue(connectors, knowledge_bases, skills, agents)


def _catalogue_block(cat: Catalogue) -> str:
    def render(rows: Iterable[dict], empty: str) -> str:
        rows = list(rows)
        if not rows:
            return f'  (none — {empty})'
        return '\n'.join('  ' + json.dumps(row, ensure_ascii=False) for row in rows)

    return (
        'CONNECTIONS this account has (ids for `connectors`):\n'
        + render(cat.connectors, 'do not set `connectors` or the `mcp` grant')
        + '\n\nKNOWLEDGE BASES (ids for `knowledgeBases`):\n'
        + render(cat.knowledge_bases,
                 'do not set `knowledgeBases` or the `rag` grant')
        + '\n\nSKILLS (ids for `skills`):\n'
        + render(cat.skills, 'do not set `skills`')
        + '\n\nAGENTS this user already has (ids for `delegatesTo`):\n'
        + render(cat.agents, 'do not set `delegatesTo` or the `subAgents` grant')
    )


def _knob_block() -> str:
    return '\n'.join(f'- {path}: {knob.describe}' for path, knob in KNOBS.items())


SYSTEM = """You configure an autonomous agent by setting its knobs.

The user is looking at a builder: a chat pane on the left, and on the right a \
board of every setting listed below. They describe what they want the agent to \
do; you reply, and you return the settings that description implies.

You are not the agent. You never do the work the user describes, and you never \
ask them to repeat what they have already written. Even a bare greeting or a \
one-line brief is enough to configure something sensible — read the CURRENT \
CONFIG, especially `brief`, which the user may have typed on the board rather \
than to you, and act on that.

Rules:
- Return ONLY the knobs that should change. Never restate a value the config \
already has.
- `brief` is the agent's system prompt. If it is blank or thin, write it: \
second person, concrete about inputs, outputs and limits, a short paragraph or \
a few lines. If the user has already written a good one, leave it alone.
- Grants are the blast radius. Turn on only what the described job needs, and \
say why in `why`.
- A connector needs BOTH the `mcp` grant and that connector's id in \
`connectors`. A knowledge base needs the `rag` grant and its id. Delegation \
needs the `subAgents` grant and ids in `delegatesTo`.
- Prefer `{"id": N, "mode": "read"}` for a connection the agent only needs to \
read from: naming a mailbox otherwise hands it sending and deleting too.
- Anything that sends, posts, pays or deletes keeps `autonomy` at `ask` unless \
the user explicitly asked to be left out of it.
- A cadence ("every morning", "each Monday") means a `schedule` and \
`allowUnattended` = true — both, or the schedule is refused at every firing. \
Set `scheduleTimezone` to THE USER'S TIMEZONE unless they named another place.
- Never name an id that is not in the catalogue below.

Answer with a single JSON object and nothing else:

{"reply": "<2-3 sentences, plain, addressed to the user>",
 "changes": [{"path": "<knob>", "value": <json>, "why": "<one short clause>"}]}

`changes` may be empty, but only when the config already says everything the \
message implies — then say so in `reply`.

THE KNOBS YOU MAY SET (anything else is ignored):
__KNOBS__

__CATALOGUE__
"""


def _prompt(message: str, cfg: dict, history: list[dict],
            user_timezone: str = 'UTC') -> str:
    turns = []
    for turn in history[-MAX_HISTORY_TURNS:]:
        role = 'User' if turn.get('role') == 'user' else 'You'
        text = str(turn.get('text', ''))[:600]
        if text:
            turns.append(f'{role}: {text}')
    # The schedule is echoed in words as well as cron, because `0 9 * * 1` and
    # `9 0 * * 1` are both valid, both plausible, and nine hours apart.
    cron = (cfg.get('schedule') or '').strip()
    schedule_note = ''
    if cron and cron_is_valid(cron):
        schedule_note = f'\n(The current schedule reads: {describe_cron(cron)}.)'
    return (
        'CURRENT CONFIG:\n'
        + json.dumps(cfg, ensure_ascii=False, indent=2, default=str)
        + schedule_note
        # From the user's Settings. Without it "every morning" became 09:00 UTC,
        # which is mid-afternoon for most of the people using this.
        + f"\n\nTHE USER'S TIMEZONE: {user_timezone}"
        + ('\n\nEARLIER IN THIS BUILDER CHAT:\n' + '\n'.join(turns) if turns else '')
        + f'\n\nTHE USER SAYS:\n{message}\n'
    )


# --------------------------------------------------------------------------
# Parsing and sanitising what came back
# --------------------------------------------------------------------------

_FENCE = re.compile(r'```(?:json)?\s*(.*?)\s*```', re.DOTALL)


def parse_reply(text: str) -> dict | None:
    """The JSON object in a model's answer, or None.

    Models wrap JSON in prose and in fences, and both are cheaper to strip here
    than to prevent with a stricter prompt.
    """
    if not text:
        return None
    candidates = [match.group(1) for match in _FENCE.finditer(text)]
    candidates.append(text)
    start, end = text.find('{'), text.rfind('}')
    if start != -1 and end > start:
        candidates.append(text[start:end + 1])
    for chunk in candidates:
        try:
            parsed = json.loads(chunk)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _merge(cfg: dict, changes: list[dict]) -> dict:
    merged = json.loads(json.dumps(cfg, default=str))
    for change in changes:
        head, _, tail = change['path'].partition('.')
        if tail:
            if not isinstance(merged.get(head), dict):
                merged[head] = {}
            merged[head][tail] = change['value']
        else:
            merged[head] = change['value']
    return merged


def sanitise(raw_changes: Any, cfg: dict, cat: Catalogue) -> list[dict]:
    """Model output -> changes we are willing to show, in the board's shape."""
    if not isinstance(raw_changes, list):
        return []
    seen: set[str] = set()
    out: list[dict] = []
    for item in raw_changes[:40]:
        if not isinstance(item, dict):
            continue
        path = item.get('path')
        knob = KNOBS.get(path) if isinstance(path, str) else None
        if knob is None or path in seen:
            continue
        try:
            value = knob.coerce(item.get('value'), cat)
        except Reject as exc:
            logger.info('agent-builder: dropped %s (%s)', path, exc)
            continue
        # A "change" that changes nothing is noise on the board: it highlights a
        # knob the user never touched and buries the ones that moved.
        head, _, tail = path.partition('.')
        current = (cfg.get(head) or {}).get(tail) if tail else cfg.get(head)
        if current == value:
            continue
        seen.add(path)
        why = str(item.get('why') or '').strip()[:200] or 'Implied by what you described.'
        out.append({'path': path, 'label': knob.label, 'value': value, 'why': why})
    return out


def _errors_for(cfg: dict, changes: list[dict], request) -> set[str]:
    """Which fields the save would refuse this config on."""
    merged = _merge(cfg, changes)
    # Name is required, and a brand-new board has none. A placeholder keeps a
    # missing name from masking the errors we are actually looking for; it is
    # never returned to the caller.
    if not str(merged.get('name') or '').strip():
        merged['name'] = 'Draft agent'
    serializer = AgentSerializer(data=merged, context={'request': request})
    return set() if serializer.is_valid() else set(serializer.errors)


def enforce_couplings(changes: list[dict], cfg: dict, request) -> list[dict]:
    """Drop whatever the save would refuse, using the save's own validator.

    A proposal the user cannot save is worse than no proposal: the board lights
    up, Save answers 400, and nothing on screen says which of the eight
    highlighted knobs is the problem. `AgentSerializer` owns the cross-field
    rules, so this asks it and removes changes until it is happy.

    Two things it has to separate. Errors the board already had — a
    half-finished config with no name, a knowledge base since deleted — are not
    ours to fix and must not silently eat every change. And a cross-field rule
    names the *other* knob: refusing a schedule without `allowUnattended`
    reports `allowUnattended`, which is precisely the path the model did not
    propose, so matching errors to changed paths finds nothing and would return
    the unsavable set. That is why the fallback drops the newest change and
    tries again rather than giving up: it converges, and it converges on the
    side of proposing less.
    """
    baseline = _errors_for(cfg, [], request)
    while changes:
        bad = _errors_for(cfg, changes, request) - baseline
        if not bad:
            return changes
        culprits = [c for c in changes if c['path'].partition('.')[0] in bad]
        if not culprits:
            culprits = [changes[-1]]
        logger.info('agent-builder: dropping %s (refused on %s)',
                    [c['path'] for c in culprits], sorted(bad))
        changes = [c for c in changes if c not in culprits]
    return []


# --------------------------------------------------------------------------
# The call
# --------------------------------------------------------------------------

def _candidates(cfg: dict) -> list[tuple[str, str]]:
    """Which (provider, model) to ask, best first.

    The agent's own model first — it is the one the user picked and holds a key
    for. The platform's summary model second, because it runs on the platform's
    own NVIDIA key: a user who has connected nothing still gets a builder that
    works, which is the difference between this feature existing and not.
    """
    pairs: list[tuple[str, str]] = []
    provider = str(cfg.get('provider') or '').strip()
    model = str(cfg.get('model') or '').strip()
    if provider and model:
        pairs.append((provider, model))
    fallback = (
        getattr(django_settings, 'AGENT_BUILDER_PROVIDER', '')
        or getattr(django_settings, 'CONTEXT_SUMMARY_PROVIDER', ''),
        getattr(django_settings, 'AGENT_BUILDER_MODEL', '')
        or getattr(django_settings, 'CONTEXT_SUMMARY_MODEL', ''),
    )
    if all(fallback) and fallback not in pairs:
        pairs.append(fallback)
    return pairs


async def _ask(cfg: dict, message: str, history: list[dict], cat: Catalogue,
               user_id: int, user_timezone: str = 'UTC') -> tuple[dict | None, str]:
    """(parsed answer, error). Never raises on a provider's behalf."""
    from llm import access as llm

    system = SYSTEM.replace('__KNOBS__', _knob_block()).replace(
        '__CATALOGUE__', _catalogue_block(cat))
    prompt = _prompt(message, cfg, history, user_timezone)
    last_error = ''
    for provider, model in _candidates(cfg):
        try:
            completion = await llm.complete(
                provider=provider, model=model, prompt=prompt,
                system_message=system, user_id=user_id,
                # Configuration is not a creative act: the same brief should
                # produce the same board twice running.
                temperature=0, max_tokens=2000,
            )
        except Exception as exc:  # provider down, no key, retired model
            last_error = str(exc)
            logger.info('agent-builder: %s/%s unavailable (%s)', provider, model, exc)
            continue
        parsed = parse_reply(completion.content)
        if parsed is not None:
            return parsed, ''
        last_error = 'The model did not answer with a configuration.'
        logger.info('agent-builder: unparseable answer from %s/%s', provider, model)
    return None, last_error


@extend_schema(
    methods=['POST'],
    request=ConfigureSerializer,
    responses={200: OpenApiResponse(
        description='A reply and the knob changes it implies')},
    description=(
        'Turn a plain-language description into agent configuration changes. '
        'Returns a proposal for the builder to highlight — nothing is saved.'
    ),
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def configure_agent(request):
    serializer = ConfigureSerializer(data=request.data)
    if not serializer.is_valid():
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)
    data = serializer.validated_data

    cfg = {
        key: value for key, value in (data['config'] or {}).items()
        if key not in {'id', 'status', 'runs', 'unattended', 'spend',
                       'created_at', 'updated_at', 'extraSchedules'}
    }
    cat = await _catalogue(request.user)
    from core.preferences import for_user as preferences_for

    prefs = await sync_to_async(preferences_for)(request.user.id)
    parsed, error = await _ask(cfg, data['message'], data['history'], cat,
                               request.user.id, prefs.timezone)

    if parsed is None:
        # 503, not 200-with-an-apology: the caller falls back to its own local
        # rules, and it can only know to do that if this says it failed.
        return Response(
            {'error': 'The configuring model could not be reached.',
             'detail': error or None,
             'code': 'builder_model_unavailable'},
            status=status.HTTP_503_SERVICE_UNAVAILABLE,
        )

    changes = sanitise(parsed.get('changes'), cfg, cat)
    changes = await sync_to_async(enforce_couplings)(changes, cfg, request)
    reply = str(parsed.get('reply') or '').strip()[:2000]
    if not reply:
        reply = (
            'I set what your description implied — each change is highlighted '
            'on the right.' if changes else 'Nothing here needed to change.'
        )
    if data.get('agent_id'):
        await _remember_turn(request.user, data['agent_id'], data['message'],
                             reply, changes)
    return Response({'reply': reply, 'changes': changes, 'source': 'model'})


#: How much of an agent's builder conversation is kept and returned. It is a
#: record of how the agent came to be configured, not an archive.
BUILDER_CHAT_KEEP = 40


@sync_to_async
def _remember_turn(user, agent_id: int, message: str, reply: str,
                   changes: list[dict]) -> None:
    """Keep one exchange of an agent's builder chat.

    The conversation lived in React state, so a reload threw away the reasoning
    behind every knob it had moved — the `why` on each change is the only place
    that reasoning is written down. Stored on `ConversationMessage` (which
    already has a `subagent` column) rather than a new table. Best-effort: a
    failed write costs the history, never the proposal the user is waiting on.
    """
    from agents.models import ConversationMessage, SubAgent

    try:
        agent = SubAgent.objects.filter(id=agent_id, user=user).first()
        if agent is None:
            return
        ConversationMessage.objects.create(
            user=user, subagent=agent, role='user', content=message,
            metadata={'surface': 'builder'},
        )
        ConversationMessage.objects.create(
            user=user, subagent=agent, role='assistant', content=reply,
            metadata={'surface': 'builder', 'changes': changes},
        )
        stale = list(
            ConversationMessage.objects
            .filter(user=user, subagent=agent, metadata__surface='builder')
            .order_by('-created_at', '-id')
            .values_list('id', flat=True)[BUILDER_CHAT_KEEP:]
        )
        if stale:
            ConversationMessage.objects.filter(id__in=stale).delete()
    except Exception:  # noqa: BLE001
        logger.warning('agent-builder: could not keep the turn', exc_info=True)


@extend_schema(
    methods=['GET'],
    responses={200: OpenApiResponse(description="The agent's builder conversation")},
    description="The builder chat for one saved agent, oldest first.",
)
@async_api_view(['GET'])
@permission_classes([IsAuthenticated])
async def builder_chat(request, agent_id: int):
    """What was said while this agent was being configured, oldest first."""
    from agents.models import ConversationMessage, SubAgent

    owned = await SubAgent.objects.filter(id=agent_id, user=request.user).aexists()
    if not owned:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    @sync_to_async
    def _read():
        rows = list(
            ConversationMessage.objects
            .filter(user=request.user, subagent_id=agent_id,
                    metadata__surface='builder')
            .order_by('-created_at', '-id')[:BUILDER_CHAT_KEEP]
        )
        rows.reverse()
        return [
            {
                'role': 'user' if row.role == 'user' else 'agent',
                'text': row.content,
                'changes': (row.metadata or {}).get('changes') or [],
            }
            for row in rows
        ]

    return Response({'messages': await _read()})
