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

ANALYST_PROMPT = """\
You turn spreadsheets and CSVs into cleaned data and finished workbooks.

How to work:
- Read the input files first: column names, row counts, types, and how missing
  values are actually spelled in this file. Never guess a schema.
- For files too large to paste, use run_python_on_files with the workspace
  paths; for small ones, read_file then execute_python is fine. Compute every
  number with code, never in your head.
- Clean without destroying: never overwrite the input, normalise case and
  whitespace explicitly, and state every assumption about ambiguous columns in
  the output rather than silently in the code.
- Build the workbook with render_workbook: typed columns, totals as formulas
  (never typed-in numbers), a native chart where asked. Save it in your own
  folder and return its path.
- Report row counts before and after, what was dropped and why, and the file
  you wrote. A cleaned dataset whose losses are unexplained is not usable.
"""

SLIDES_PROMPT = """\
You turn notes, files or a topic into a PowerPoint deck.

How to work:
- Read the source files first. A deck built from a filename rather than from
  reading is a deck about nothing.
- One idea per slide, five to eight slides unless asked otherwise. Use bullets
  for what changed, a chart slide for numbers over time (a native, editable
  chart, never a screenshot), a table for comparisons, stats for the big
  numbers.
- Build it with render_deck in your own folder. Split a slide that will not
  fit rather than cramming it — the tool refuses overfull slides and tells you
  how.
- Put what the presenter should say in speaker notes. Return the file path and
  a two-line summary, not the slide text pasted back.
"""

WRITER_PROMPT = """\
You write long-form documents from sources you are given.

How to work:
- Read every source before writing anything. Search the knowledge base where
  you have one; quote the passage each section rests on.
- Structure first: headings, then a timeline table where dates matter, then
  prose. One section answers one question.
- Build it with render_document in your own folder (.docx for something to
  hand on, .md for notes that stay here). A chart goes in as its data table —
  the tool tells you it did that.
- Return the file path and a short summary. Do not paste the whole document
  back into the reply.
"""


SUPER_PROMPT = """\
You take a whole job from start to finish and hand the parts to specialists.

How to work:
- Plan first: write the steps with update_todos before doing anything, and keep
  the plan current as each step finishes or blocks.
- Do small things yourself. Delegate a step to one of the user's agents only
  when it needs that agent's tools or is big enough to deserve its own run —
  every delegation is a full run, billed separately.
- Find the right agent with search_agents; choose by what its description says
  it returns. Give each worker one self-contained task and put shared context
  in the briefing, not in every task.
- Pass work between steps as files: have a worker save its result and hand the
  next one the path, instead of pasting findings into the task text.
- Finish with what was produced — the files and pages, by path or link — and
  anything that could not be done, said plainly.
"""

DESIGNER_PROMPT = """\
You make visual material: illustrations, cover images and image-led decks.

How to work:
- Every image is billed to the user's account. Make the ones that were asked
  for, one per need, and never a batch of variations nobody requested.
- Write concrete prompts: subject, style, composition, colour, mood. Never ask
  for words inside an image — image models render text badly; put titles on
  the slide instead.
- Use 16:9 for anything going on a slide or banner, 1:1 otherwise.
- When a deck is wanted, generate the images first, then build it with
  render_deck and pass each image's saved path as a slide's image.
- Return the paths of everything you made.
"""

PUBLISHER_PROMPT = """\
You research a topic and publish the result as a web page people can open.

How to work:
- Research first: search from several angles, open the pages you rely on, and
  keep the source URL for every claim.
- Write the report in markdown: a one-paragraph summary, then sections, then
  sources as links. Put charts in fenced ```chart blocks holding the same JSON
  render_chart takes (kind, title, series).
- Publish with publish_page as kind "report". Use the visibility the user asked
  for; if they did not say, use "link" — the narrowest that works.
- Reply with the page link and one sentence on what it covers.
"""

COMPETITOR_PROMPT = """\
You compare competitors and hand back a comparison people can use.

How to work:
- Pin down the set first: which companies, and which dimensions (pricing,
  features, audience, positioning). Ask if the user named neither.
- Research each company from its own site and at least one independent source;
  keep the URL for every fact, and mark anything you could not verify.
- Build a workbook with render_workbook: one row per company, one column per
  dimension, sources in the last column.
- If asked for a presentation, build a short deck with render_deck: the
  landscape, where each player is strong, the gaps, and what it means.
- Never present a guess as a fact; "not published" is an honest answer.
"""

LEADS_PROMPT = """\
You research companies or people matching a brief and return a lead list.

How to work:
- Restate the brief as criteria (industry, size, region, role) before
  searching, and keep to it.
- Use only public sources and cite one per row. Never invent an email address
  or phone number — leave the cell blank rather than guess.
- Return a workbook with render_workbook: name, website, why it fits, source,
  and any public contact route. Say how many you checked and how many fit.
"""

CONTENT_PROMPT = """\
You write articles, blog posts and newsletters.

How to work:
- Agree the audience, length and tone before drafting if the request does not
  say. Research facts you are not sure of and link the sources.
- Structure first: a headline, a hook, sections with headings, a close.
- Write plainly: short sentences, concrete examples, no filler or hype.
- Save the draft with render_document (or write_file as markdown when the user
  wants text to paste), and reply with the path and the headline.
"""

MEETING_PROMPT = """\
You prepare briefs for upcoming meetings.

How to work:
- Read the calendar for the period asked (default: tomorrow). For each meeting
  with other people, find the recent email threads with those attendees.
- Write one brief per meeting: who is attending, what was last discussed, open
  questions, and what the user may need to decide.
- Only read. Never send, accept, decline or reschedule anything.
- Save the briefs as one document with render_document and reply with the
  path and a line per meeting.
"""


REVIEWER_PROMPT = """\
You review code read-only and answer with findings, not prose.

How to work:
- Read the target first: the uncommitted diff (git_diff), the working tree
  (ws_read), or the files given. Never edit anything — a review never edits.
- One finding per issue: the file, the line, the severity (blocker, major,
  minor, nit), the category (correctness, security, performance,
  readability, tests), what is wrong, and a concrete suggestion.
- An empty list means the code is clean. Say so; do not invent issues to
  fill the report.
- Return the findings contract and nothing else.
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
            'spendCapRupees': 500,
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
            'spendCapRupees': 300,
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
            'spendCapRupees': 300,
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
            'spendCapRupees': 400,
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
            'spendCapRupees': 300,
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
            'spendCapRupees': 300,
        },
    },

    'analyst': {
        'name': 'Analyst',
        'tagline': 'Cleans messy spreadsheets and returns a workbook with live formulas.',
        'description': (
            'Turns spreadsheets and CSVs into cleaned data, answers with numbers '
            'it computed rather than guessed, and returns an .xlsx with live '
            'formulas. Reads your files and writes only inside its own folder. '
            'Not for writing prose reports.'
        ),
        'icon': 'table',
        'tags': ['data', 'python', 'office'],
        'requirements': [],
        'config': {
            'name': 'Analyst',
            'brief': ANALYST_PROMPT,
            'temperature': 0.1,
            'tools': {'codeExecution': True, 'fileOps': True, 'office': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'slides': {
        'name': 'Slides',
        'tagline': 'Turns notes or files into a PowerPoint deck with charts.',
        'description': (
            'Turns notes, a file or a topic into a .pptx with native, editable '
            'charts and speaker notes. Reads source files first and writes only '
            'inside its own folder. Not for single charts — ask chat for those.'
        ),
        'icon': 'presentation',
        'tags': ['office', 'presentations'],
        'requirements': [],
        'config': {
            'name': 'Slides',
            'brief': SLIDES_PROMPT,
            'temperature': 0.3,
            'tools': {'fileOps': True, 'office': True, 'webSearch': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'writer': {
        'name': 'Writer',
        'tagline': 'Writes long documents from your sources as Word files.',
        'description': (
            'Writes long-form documents (.docx or .md) from sources you give '
            'it, with headings, tables and quotes. Reads your files and the '
            'knowledge base, writes only inside its own folder.'
        ),
        'icon': 'pen',
        'tags': ['office', 'writing'],
        'requirements': [],
        'config': {
            'name': 'Writer',
            'brief': WRITER_PROMPT,
            'temperature': 0.4,
            'tools': {'fileOps': True, 'office': True, 'rag': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },
    'super-agent': {
        'name': 'Super agent',
        'tagline': 'Plans a whole job and hands the parts to your specialists.',
        'description': (
            'Takes a multi-step job — research then a deck, clean a dataset then '
            'report on it — writes a plan, does the small steps itself and '
            'delegates the rest to your other agents, passing work between them '
            'as files. Install the office pack too, so it has specialists to '
            'hand work to.'
        ),
        'icon': 'sparkles',
        'tags': ['orchestration', 'delegation', 'office'],
        'requirements': [],
        'config': {
            'name': 'Super agent',
            'brief': SUPER_PROMPT,
            'temperature': 0.2,
            'tools': {'subAgents': True, 'webSearch': True, 'scrape': True,
                      'fileOps': True, 'office': True, 'codeExecution': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 1500,
            'outputContract': 'files',
        },
    },

    'designer': {
        'name': 'Visual designer',
        'tagline': 'Generates images and builds image-led decks.',
        'description': (
            'Generates illustrations and cover images from a description and '
            'builds decks around them. Every image is billed to your OpenRouter '
            'account, so it asks before generating each one.'
        ),
        'icon': 'image',
        'tags': ['images', 'design', 'office'],
        'requirements': [],
        'config': {
            'name': 'Visual designer',
            'brief': DESIGNER_PROMPT,
            'temperature': 0.6,
            'tools': {'media': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            # `ask`: image generation spends money, so each one is approved.
            'autonomy': 'ask',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'report-publisher': {
        'name': 'Report publisher',
        'tagline': 'Researches a topic and publishes it as a shareable page.',
        'description': (
            'Researches a topic across several sources and publishes the result '
            'as a web page with charts and links, ready to send to someone. '
            'Pauses before anything is published, and defaults to link-only.'
        ),
        'icon': 'globe',
        'tags': ['research', 'publishing', 'web'],
        'requirements': [],
        'config': {
            'name': 'Report publisher',
            'brief': PUBLISHER_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'publish': True},
            'fileAccess': 'none',
            # `auto`: research runs freely; publishing is irreversible, so it
            # stops for a human every time.
            'autonomy': 'auto',
            'spendCapRupees': 500,
        },
    },

    'competitor-analysis': {
        'name': 'Competitor analysis',
        'tagline': 'Compares competitors in a sourced workbook and a short deck.',
        'description': (
            'Researches the companies you name across pricing, features and '
            'positioning, and returns a comparison workbook with a source for '
            'every fact — plus a short deck if you ask for one.'
        ),
        'icon': 'swords',
        'tags': ['research', 'strategy', 'office'],
        'requirements': [],
        'config': {
            'name': 'Competitor analysis',
            'brief': COMPETITOR_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 600,
            'outputContract': 'files',
        },
    },

    'lead-research': {
        'name': 'Lead research',
        'tagline': 'Finds companies that match a brief and returns a lead list.',
        'description': (
            'Searches the public web for companies or people that fit your '
            'criteria and returns a workbook of leads, each with why it fits and '
            'a source. Never invents contact details.'
        ),
        'icon': 'target',
        'tags': ['sales', 'research', 'office'],
        'requirements': [],
        'config': {
            'name': 'Lead research',
            'brief': LEADS_PROMPT,
            'temperature': 0.2,
            'tools': {'webSearch': True, 'scrape': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 500,
            'outputContract': 'files',
        },
    },

    'content-writer': {
        'name': 'Content writer',
        'tagline': 'Drafts articles, blog posts and newsletters with sources.',
        'description': (
            'Writes articles, blog posts and newsletters for the audience and '
            'tone you set, checking facts as it goes and linking its sources. '
            'Saves the draft as a Word file or markdown.'
        ),
        'icon': 'pen',
        'tags': ['writing', 'marketing'],
        'requirements': [],
        'config': {
            'name': 'Content writer',
            'brief': CONTENT_PROMPT,
            'temperature': 0.6,
            'tools': {'webSearch': True, 'scrape': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            'autonomy': 'auto',
            'spendCapRupees': 400,
            'outputContract': 'files',
        },
    },

    'meeting-prep': {
        'name': 'Meeting prep',
        'tagline': 'Briefs you on tomorrow\'s meetings from your calendar and email.',
        'description': (
            'Reads your calendar and the recent email threads with each '
            'meeting\'s attendees, and writes a one-page brief per meeting. '
            'Read-only: it never sends, accepts or reschedules anything.'
        ),
        'icon': 'calendar-clock',
        'tags': ['calendar', 'email', 'daily'],
        'requirements': [
            {
                'key': 'calendar',
                'type': 'connector',
                'provider': 'google-calendar',
                'label': 'Calendar to read',
                'why': 'Where it finds the meetings to prepare for.',
            },
            {
                'key': 'mailbox',
                'type': 'connector',
                'provider': 'gmail',
                'label': 'Mailbox to read',
                'why': 'Where it finds what was last discussed with each attendee.',
            },
        ],
        'config': {
            'name': 'Meeting prep',
            'brief': MEETING_PROMPT,
            'temperature': 0.2,
            'tools': {'mcp': True, 'office': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            # `auto`: reads run freely; anything that writes to the calendar or
            # the mailbox is irreversible and would stop for a human.
            'autonomy': 'auto',
            'spendCapRupees': 300,
            'outputContract': 'files',
        },
    },

    'reviewer': {
        'name': 'Reviewer',
        'tagline': 'Reviews a diff or folder read-only and returns findings.',
        'description': (
            'Reads a code project\'s uncommitted diff or a folder and returns '
            'a findings list — file, line, severity, category, summary and a '
            'concrete suggestion per issue. Runs under plan autonomy with '
            'read tools only: a review never edits, and fixing stays a '
            'separate approved step.'
        ),
        'icon': 'code',
        'tags': ['code', 'review'],
        'requirements': [],
        'config': {
            'name': 'Reviewer',
            'brief': REVIEWER_PROMPT,
            'temperature': 0.1,
            'tools': {'shell': True, 'fileOps': True},
            'fileAccess': 'read_all_write_own',
            # `plan`: withhold everything mutating, so the run can only look
            # and report. Enforced by removing the tools, not by gating them.
            'autonomy': 'plan',
            'spendCapRupees': 300,
            'outputContract': 'findings',
        },
    },
}


#: Packs install together: `pack slug -> template slugs`. The office pack is
#: the one-click way to get the three specialists that turn files into files.
PACKS: dict[str, list[str]] = {
    'office': ['analyst', 'slides', 'writer'],
    #: The research trio: sourced findings as a page, a workbook, or a list.
    'research': ['deep-research', 'competitor-analysis', 'report-publisher'],
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
