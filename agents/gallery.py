"""
The template gallery: agent configurations you can install and then edit.

**A template is code, not a row.** `templates/models.py` says why — an agent
template is a `SubAgent` used as a starting point, and a table would buy
nothing a dict does not: nobody edits a curated template through the admin,
every field it carries is already a column on the thing it installs into, and a
migration-seeded row would drift from the serializer that validates it. So the
catalogue lives here, next to `stock.py`, which is the same idea for the
configurations the runtime itself delegates to.

**A template stores the flat `AgentConfig`, not model columns.** Install hands
that dict straight to `AgentSerializer`, which is the one mapping between the
wire shape and the columns. The alternative — a second dict in column shape,
written by hand — is a second mapping, and the failure it produces is the one
`docs/AGENT_TEMPLATES.md` §5 calls unforgivable: a permissions screen that
promises something the runtime never checks. Here the screen renders the same
`tools` / `guardrails` keys the serializer stores and the runtime reads,
because there is only one copy of them.

**A template names requirements, never ids.** A config that pointed at
knowledge base 2 would, installed elsewhere, either break or silently read
somebody else's row 2. So `requirements` is a portable list — *what kind* of
connection or corpus the agent needs and what it is for — and the installer
satisfies each one with something they own. The resolved ids land on the
installed agent and nowhere else. Credentials never travel: the template names
the kind of connection, the installer supplies their own.

Consequently `config` here must never contain `connectors`, `knowledgeBases`
or `skills`. `check_catalogue` fails the test suite if one does.
"""
from __future__ import annotations

from typing import Any

#: The requirement kinds an installer can satisfy, mapped to the `AgentConfig`
#: list each resolves into. Closed on purpose: a requirement of an unknown kind
#: would render on the install screen as a dropdown with nothing behind it.
REQUIREMENT_FIELDS: dict[str, str] = {
    'connector': 'connectors',
    'knowledge_base': 'knowledgeBases',
    'skill': 'skills',
}


TRIAGE_PROMPT = """\
You triage a mailbox and say what needs a person.

How to work:
- Read what has arrived since you last ran. Do not re-read what you already
  reported on.
- Sort each message into one of: needs a reply from the owner, needs an action
  but not a reply, or needs nothing.
- For anything needing a reply, draft one. Draft it, do not send it — the
  owner decides what leaves their account.
- Quote the sentence that made you classify a message the way you did. A
  summary the owner cannot check against the original is worse than the
  original.
- Say plainly when a message is ambiguous rather than guessing at intent.
"""

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

DOCS_PROMPT = """\
You answer questions from a specific set of documents, and only from them.

How to work:
- Search the knowledge base before answering anything. Your own recollection
  is not a source here.
- Quote the passage your answer rests on, and name the document it came from.
- If the documents do not answer the question, say so. Do not fill the gap
  from general knowledge — the whole value of this agent is that its answers
  are checkable against the corpus.
- When two documents disagree, report both and say which is more recent.
"""

REPORT_PROMPT = """\
You write a short weekly report on what changed, for someone who was not
watching.

How to work:
- Gather first, write second. Collect the material before deciding what the
  story is.
- Lead with what changed, not with what happened. A list of events is not a
  report.
- Three to six points. If everything is worth reporting, nothing is.
- Quantify where you can and say the number's source. Where you cannot, say
  the claim is qualitative rather than dressing it up.
- Save the report as a dated markdown file in your own folder, and reply with
  the same text so it can be read without opening anything.
"""

DATA_PROMPT = """\
You clean and summarise tabular data.

How to work:
- Look at the file before deciding anything: column names, row count, types,
  and how missing values are actually spelled in this file.
- Write Python to do the work. Do not describe transformations you have not
  run — run them and report what the code returned.
- State every assumption you had to make about ambiguous columns, and make it
  visible in the output rather than silently in the code.
- Never overwrite the input. Write results to a new file.
- Report the row counts before and after, and what was dropped and why. A
  cleaned dataset whose losses are unexplained is not usable.
"""

WATCH_PROMPT = """\
You watch a small set of sources and report only what is new.

How to work:
- Check each source you have been given. Read the pages; a title is not a
  change.
- Compare against what you reported last time. Say "nothing new" when there is
  nothing new — a report padded with restated old material teaches the reader
  to stop opening it.
- For each genuine change: what changed, when, the source link, and one line
  on why it might matter.
- Do not speculate about intent or consequence beyond one sentence, and mark
  it as speculation when you do.
"""


#: slug -> the gallery entry. `config` is a flat `AgentConfig`; anything it
#: omits takes the serializer's default, which is the cautious end of every
#: dial.
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
            'egress': 'none',
            'spendCapRupees': 500,
            'trigger': 'goal',
        },
    },

    'inbox-triage': {
        'name': 'Inbox triage',
        'tagline': 'Sorts what arrived, drafts the replies, and says what needs you.',
        'description': (
            'Reads the mailbox you point it at, sorts each message by whether '
            'it needs you, and drafts replies for the ones that do. It drafts '
            'and never sends: the autonomy level stops it before anything '
            'leaves your account, so every outgoing message is still yours to '
            'approve.'
        ),
        'icon': 'inbox',
        'tags': ['email', 'triage', 'daily'],
        'requirements': [
            {
                'key': 'mailbox',
                'type': 'connector',
                'provider': 'gmail',
                'label': 'Mailbox to triage',
                'why': 'It reads arriving mail and drafts replies here.',
            },
        ],
        'config': {
            'name': 'Inbox triage',
            'brief': TRIAGE_PROMPT,
            'temperature': 0.2,
            'tools': {'mcp': True},
            'fileAccess': 'none',
            # The one level that matches "draft, never send": it runs the reads
            # without asking and stops at anything leaving the account.
            'autonomy': 'ask',
            'notifyOnHitl': True,
            'egress': 'none',
            'spendCapRupees': 300,
            'trigger': 'goal',
        },
    },

    'document-qa': {
        'name': 'Document Q&A',
        'tagline': 'Answers strictly from a knowledge base you choose, with quotes.',
        'description': (
            'Answers questions from one corpus and refuses to answer from '
            'anywhere else, quoting the passage and naming the document each '
            'time. Scoped to the knowledge base you pick at install — it '
            'cannot read your others, so an answer from the wrong corpus '
            'cannot look like an answer from the right one.'
        ),
        'icon': 'book-open',
        'tags': ['knowledge', 'support'],
        'requirements': [
            {
                'key': 'corpus',
                'type': 'knowledge_base',
                'label': 'Documents to answer from',
                'why': 'The only source it is allowed to answer from.',
            },
        ],
        'config': {
            'name': 'Document Q&A',
            'brief': DOCS_PROMPT,
            'temperature': 0.1,
            'tools': {'rag': True},
            'fileAccess': 'none',
            'autonomy': 'full',
            'egress': 'none',
            'spendCapRupees': 300,
            'trigger': 'goal',
        },
    },

    'weekly-report': {
        'name': 'Weekly report',
        'tagline': 'Runs every Monday morning and writes up what changed.',
        'description': (
            'A scheduled agent: it gathers the material, decides what the '
            'story is, and saves a dated report to its own folder as well as '
            'replying with the text. Installed with a Monday 09:00 schedule in '
            'your timezone, which you can change or clear in the builder. It '
            'can read your files and write only inside its own folder.'
        ),
        'icon': 'calendar-clock',
        'tags': ['reporting', 'scheduled'],
        'requirements': [
            {
                'key': 'corpus',
                'type': 'knowledge_base',
                'label': 'Material to report on',
                'why': 'What it reads to work out what changed.',
                'optional': True,
            },
        ],
        'config': {
            'name': 'Weekly report',
            'brief': REPORT_PROMPT,
            'temperature': 0.3,
            'tools': {'rag': True, 'fileOps': True, 'webSearch': True},
            # Reads the whole tree, writes only its own folder — so the report
            # lands somewhere you can open and nothing else can be overwritten.
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'egress': 'none',
            'spendCapRupees': 400,
            'trigger': 'maintenance',
            'schedule': '0 9 * * 1',
            # Required alongside a schedule: without it the sweep's every
            # firing is refused. See `AgentSerializer.validate`.
            'allowUnattended': True,
        },
    },

    'data-cleanup': {
        'name': 'Data cleanup',
        'tagline': 'Inspects a messy spreadsheet, cleans it, and says what it dropped.',
        'description': (
            'Looks at the file first, writes Python to do the work, and '
            'reports row counts before and after with the reason for every '
            'loss. It never overwrites the input, and the sandbox it runs code '
            'in has no network access at all.'
        ),
        'icon': 'table',
        'tags': ['data', 'python'],
        'requirements': [],
        'config': {
            'name': 'Data cleanup',
            'brief': DATA_PROMPT,
            'temperature': 0.1,
            'tools': {'codeExecution': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            # Code execution with no way to dial out. The combination is the
            # point: it can compute on your data and cannot post it anywhere.
            'egress': 'none',
            'spendCapRupees': 300,
            'trigger': 'goal',
        },
    },

    'source-watch': {
        'name': 'Source watch',
        'tagline': 'Checks the pages you care about and reports only what changed.',
        'description': (
            'Watches a small set of sources and reports the differences — and '
            'says "nothing new" when there is nothing new, which is the '
            'behaviour that makes a recurring report worth opening. Installed '
            'without a schedule; add one in the builder once you have told it '
            'which sources to watch.'
        ),
        'icon': 'radar',
        'tags': ['monitoring', 'web'],
        'requirements': [],
        'config': {
            'name': 'Source watch',
            'brief': WATCH_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'fileOps': True},
            # It needs somewhere to keep what it reported last time; its own
            # folder is enough, and is all it gets.
            'fileAccess': 'scoped',
            'autonomy': 'auto',
            'egress': 'none',
            'spendCapRupees': 300,
            'trigger': 'goal',
        },
    },
}


#: The keys a template's `config` may never carry — they point at rows in the
#: author's account, and `requirements` is how a template asks for them
#: portably instead.
_ID_BEARING_KEYS = frozenset(REQUIREMENT_FIELDS.values())


def get(slug: str) -> dict[str, Any] | None:
    """The catalogue entry for `slug`, or None."""
    entry = TEMPLATES.get(slug)
    if entry is None:
        return None
    return {'slug': slug, **entry}


def listing() -> list[dict[str, Any]]:
    """Every template, in catalogue order."""
    return [{'slug': slug, **entry} for slug, entry in TEMPLATES.items()]


def check_catalogue() -> list[str]:
    """Every way the catalogue is malformed, as messages. Empty means sound.

    Called by the tests rather than at import: a broken template should fail a
    test run, not stop the server booting. The rules it enforces are the ones
    that make a template portable at all — see the module docstring.
    """
    problems: list[str] = []
    for slug, entry in TEMPLATES.items():
        for field in ('name', 'tagline', 'description', 'config'):
            if not entry.get(field):
                problems.append(f'{slug}: missing {field}')

        config = entry.get('config') or {}
        leaked = _ID_BEARING_KEYS & set(config)
        if leaked:
            problems.append(
                f'{slug}: config carries {sorted(leaked)}, which are row ids '
                f'from whoever wrote it. Ask for them in `requirements`.'
            )
        if config.get('name') != entry.get('name'):
            problems.append(f'{slug}: config name does not match the card name')

        keys: set = set()
        for req in entry.get('requirements') or []:
            for field in ('key', 'type', 'label', 'why'):
                if not req.get(field):
                    problems.append(f'{slug}: requirement missing {field}')
            if req.get('type') not in REQUIREMENT_FIELDS:
                problems.append(f'{slug}: unknown requirement type {req.get("type")!r}')
            if req.get('key') in keys:
                problems.append(f'{slug}: duplicate requirement key {req.get("key")!r}')
            keys.add(req.get('key'))

        # A requirement nothing can use is a dropdown that installs a capability
        # the agent was not granted. Both directions matter: asking for a
        # mailbox without the `mcp` grant, and asking for a corpus without
        # `rag`.
        tools = config.get('tools') or {}
        kinds = {req.get('type') for req in entry.get('requirements') or []}
        if 'connector' in kinds and not tools.get('mcp'):
            problems.append(f'{slug}: asks for a connector but has no `mcp` grant')
        if 'knowledge_base' in kinds and not tools.get('rag'):
            problems.append(f'{slug}: asks for a knowledge base but has no `rag` grant')
        if config.get('schedule') and not config.get('allowUnattended'):
            problems.append(f'{slug}: has a schedule but is not cleared to run unattended')
    return problems
