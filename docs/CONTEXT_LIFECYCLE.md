# Context lifecycle

How a long agent run stays inside its model's context window: what it keeps,
what it drops, how it gets any of it back — and what it is allowed to pull in to
begin with.

The three toggles in the agent builder — **Auto-compact history**
(`compaction`), **Smart summarization** (`recursiveContext`) and **Save and
recall** (`indexing`) — were stored, validated and round-tripped from the day
the builder shipped, and read by nothing. This is what they do now.

---

## 1. The problem

An agent run does not have a conversation; it has a transcript, and the
transcript only grows. `agent_node` sends `history + to_wire(state["messages"])`
on every iteration, `iteration_limit('research')` allows up to 40 of them, and
each tool result may be up to `TOOL_OUTPUT_CHAR_LIMIT` (64k chars ≈ 16k tokens).
Three large results exhaust the whole input budget on their own.

Before this, the only thing standing between that and the provider was
`llm.clamp_input` — the byte guard of last resort. It had three faults, each of
which is now closed:

| Fault | Consequence |
|---|---|
| Dropped one *message* at a time | An assistant turn could leave while the `tool` messages answering it stayed, producing a `tool_call_id` referring to no call in the request. Providers answer that with a 400, so an overlong run did not degrade — it died, after the user had paid for the tool calls |
| Counted only `content` | A tool-calling assistant entry has `content: None` and carries its payload in `tool_calls[].arguments`. Those entries measured as zero, so the budget the guard checked was not the size of the request |
| A flat `MAX_LLM_INPUT_TOKENS` for every model | 96k applied identically to a 200k model and an 8k one. For the small ones the guard passed and the provider rejected the request anyway |

And the escape hatches named in its own notices did not exist for an agent:
`clamp_input` said "call `search_conversation_history`", `tool_output.bound`
said "call `read_tool_output`", and `AgentToolbox` offered neither, because it
filters `AVAILABLE_TOOLS` by the names its grants unlock and no grant named
them. An escape hatch nobody can open is worse than none — the model stops
looking for another way out.

---

## 2. Shape

```
tools ──▶ curate ──▶ steering ──▶ agent ──▶ (tools | END)
            │
            ├── compaction   results → records          (free)
            ├── fold         oldest steps → one note    (one cheap model call)
            └── archive      everything removed → ToolOutput
                                                   ▲
                          recall_context / read_tool_output
```

`chat/turn/curation.py` decides; `chat/turn/agent.py::curate_node` applies;
`llm/budget.py` does the arithmetic and owns the notion of a segment;
`chat/tools/tool_output.py` stores and searches.

### It edits state, not the wire

`curate_node` returns replacement messages carrying **the ids they replace**, so
`add_messages` substitutes them in the checkpoint, and `RemoveMessage` for what
is folded away. Curating the outgoing copy instead would recompute — and
re-archive — the same text on every remaining turn of the run.

### Segments, never messages

An assistant turn carrying `tool_calls` and the `tool` messages answering it are
one indivisible unit. Everything here moves in segments: `llm.budget.Segment`
for wire dicts (used by `clamp_input`), `curation._Segment` for LangChain
messages (used by the curator). Two groupers rather than one because ids cannot
survive a messages → wire → messages round trip, and the id *is* the mechanism.

### A watermark, not a trickle

Nothing happens until the projected request crosses
`CONTEXT_HIGH_WATER_RATIO` (0.70) of the budget; then enough is cut to reach
`CONTEXT_LOW_WATER_RATIO` (0.45) in one pass. Shaving a little every turn would
change the request prefix on every single call and forfeit provider prefix
caching — the same reason the clock does not live in the system message. Between
watermarks the prefix is byte-identical.

### The last three segments are never touched

`CONTEXT_KEEP_RECENT_SEGMENTS`. Counted in segments rather than tokens so one
oversized recent result cannot push three whole turns out of view. A model that
cannot see what it just did, does it again.

---

## 3. The three mechanisms

### compaction — mechanical, free

An old tool-calling turn keeps its reasoning and the *names and arguments* of
what it called. Each result over `CONTEXT_TOOL_RECORD_CHARS` (400) is replaced
by a record:

```
[COMPACTED RESULT] web_search({"query": "q3 revenue"})
The quarterly revenue figure was 4.8 million…
[... 29,600 characters removed to stay inside the context window. archived as
tool_output id 'a1b2c3d4' — call recall_context or read_tool_output to read it.]
```

What each step *did* stays legible; what it *returned* becomes a pointer. No
model call, so this runs first and usually suffices.

### recursiveContext — one cheap model call per fold

When compaction alone cannot reach the low mark, the oldest segments are folded
into a single running note (`[EARLIER WORK IN THIS RUN — SUMMARY]`), appended as
a `SystemMessage`. Recursive in the sense that matters: the previous note is
always absorbed into the new one — including when it is sitting in the protected
tail, which is exactly where the last pass left it. **There is exactly one
note**; a second is not more memory, it is the same run written down twice.

**Which model folds.** Three levels, each a narrower statement of intent than
the next:

1. the agent's own `summaryModel` / `summaryProvider`, chosen in the builder
2. the platform default — `nvidia/nemotron-3.5-lightning-30b-a3b` on NVIDIA
3. the run's own model, as a last resort

Level 2 is NVIDIA on purpose: it is the provider the platform ships a key for
(`credentials.resolution.PLATFORM_ENV_KEYS`), so the fold works on a fresh
install and for a user who has connected nothing of their own. A fold that
silently stops working when a key is missing is worse than one that costs a
little — the run just goes back to losing its oldest steps with no summary
behind them. For the same reason level 3 is not only a configuration fallback
but a *runtime* one: if the chosen model raises `LLMUserActionable` (no
credential, no credit, retired), the fold is retried once on the run's own
model, which this turn has already proved works.

It is not the agent's own model by default because a forty-turn run on an
expensive model would pay full rate to compress itself, and the fold is an
extractive job — keep these figures, drop this prose — that a larger model is
not better at.

Its tokens are added to `total_tokens`, so the fold counts against the run's
spend cap. A summariser that spent money invisibly would be a hole in the
guardrail it serves.

### indexing — what makes the other two safe

Everything the other two remove is written to `ToolOutput` first — the same
table, retention and ownership rules as an oversized tool result, because "what
did this run drop" and "what did a tool return too much of" are the same
question to a model that has lost the text. The record left behind names the id.

With indexing **off**, the notices say so explicitly (`not archived, so the
removed text is gone`) rather than pointing at an id nobody wrote. A notice that
promises a recovery which does not exist is worse than one that admits the loss.

Retrieval is `recall_context(query)` — keyword search over everything this run
stored, returning windows with their ids — and `read_tool_output(id, offset)` to
page any of them in full. Both are in `RETRIEVAL_TOOLS`: **dispatchable always,
offered only once the run has stored something**. The split is deliberate — the
condition is a database read, and a model that names a tool it was offered a
moment ago must not be turned away because a row expired in between.

---

## 4. What is recorded

A curation pass is not an `AgentTurn`: `(execution, index)` is unique on that
table and a curation has no place in the model's turn numbering — inventing an
index for it would either collide with a real turn or renumber the ones after
it. Instead:

- `AgentRunStream.on_curation` broadcasts `context_curated` on the execution
  channel (before/after tokens, what was compacted, what was folded)
- the run's totals land in `ExecutionLog.output_data['context_curation']`, and
  only when a pass actually happened — a key reading all zeroes on every short
  run would make the interesting case harder to spot

Curation that leaves no trace is indistinguishable from a model that quietly
forgot.

---

## 4a. Acquisition: what a run may pull *in*

Retention (everything above) is half of context management. The other half is
what reaches the window in the first place, and the same defect lived there: a
control on the configuration screen that the runtime did not read.

### Knowledge bases

The builder's KB selection (`agent_context['knowledgeBases']`) was used to print
names into the system prompt and for nothing else. `knowledge_base_search`
resolved **any** KB the user owned, and with `kb_id` omitted it fell through to
the user's *default* KB — which need not be one of the agent's. An answer from
the wrong corpus looks exactly like an answer from the right one.

It is now a scope, built the same way `FileScope` is and carried the same way:
`kb_scope_for(gathered)` → `TurnContext.kb_scope` → the tool context → every KB
tool. Three rules:

- **`None` is unrestricted, and an empty selection is `None`.** Chat has no
  selection to make, and an agent built before enforcement never had one
  applied — turning it on must not silently empty its corpus.
- **No `kb_id` resolves within the scope**: the single KB if there is one,
  otherwise a request to name one. Never the user's default.
- **All five tools, including `read_document`.** It addresses a *document* id,
  not a KB, so a scope enforced in the other four would be one call away from
  irrelevant.

The prompt now carries id, backend and doc count per KB rather than a list of
names, because the id is what the tools take (a name-only prompt cost a whole
`list_knowledge_bases` turn to rediscover what the configuration already held)
and the backend decides which tool can read it at all — a semantic search
against a keyword-only index returns *nothing*, not an error, which a model
reads as "the KB has nothing on this".

`GRANT_TOOLS['rag']` unlocks all five for the same reason: it unlocked two,
while `list_knowledge_bases`' own description told the model to use
`keyword_search` on a keyword KB and `list_documents` + `read_document` on a raw
one. The catalogue was instructing the agent to call tools it would be refused,
and an agent whose KB was keyword- or raw-backed could not read it at all.

### Delegation

Results have been bounded since the fan-out existed; **instructions were not** —
and instructions are the direction that multiplies by worker count, since a task
is copied into every worker's window. Three additions
(`agents/agent/orchestrator.py`):

- `check_delegation_payload` caps task length
  (`DELEGATION_TASK_CHAR_LIMIT`), briefing length and worker count
  (`MAX_WORKERS_PER_FANOUT` — `MAX_PARALLEL_WORKERS` capped concurrency only, so
  fifty tasks still meant fifty full runs). **Refused, not truncated**: a
  trimmed instruction is a worker confidently doing the wrong job, and unlike a
  tool result arriving from outside, the author is a model that can be told to
  shorten it and try again. Refused before any worker starts.
- **`briefing`** — one shared block sent to every worker once, instead of the
  same background pasted into all N tasks. It lands in the worker's system
  prompt as *context, not instruction*: a worker that treats its briefing as the
  job does the wrong one.
- **Read-through to the parent's archive.** `TurnContext.archive_scopes` carries
  the parent's session key, one hop, read-only, and `recall_context` /
  `read_tool_output` search it alongside the worker's own. Without it curation
  and delegation work against each other: the parent curates a detail away, then
  delegates a task needing it, and the worker — a fresh thread — cannot reach
  the very text the parent could no longer restate. Writing still goes to the
  caller's own scope alone, and ownership is checked on every query: a scope is
  a session key, not a permission.

---

## 5. Chat is unchanged

`TurnContext.curation` defaults to `None` and every chat turn leaves it there,
so `curate_node` returns `{}` without reading state or touching the database.
Chat's history is already bounded by `HISTORY_WINDOW` and its long answers by
`context_summary`; its transcript is one turn deep. The agent runtime builds a
real policy from `SubAgent.runtime_settings`. Callers differ in configuration,
never in code path.

All three toggles off is a request to be left alone and is honoured: the run
falls back to `clamp_input` alone — now segment-aware, so "off" means the old
behaviour minus the 400, not an unbounded window.

---

## 6. Settings

| Name | Default | What it does |
|---|---|---|
| `CONTEXT_SUMMARY_PROVIDER` | `nvidia` | Provider for the fold when the agent names none |
| `CONTEXT_SUMMARY_MODEL` | `nvidia/nemotron-3.5-lightning-30b-a3b` | Model for the fold; the agent's `summaryModel` overrides it, blank on both falls back to the run's own |
| `CONTEXT_HIGH_WATER_RATIO` | 0.70 | Of the input budget: start curating |
| `CONTEXT_LOW_WATER_RATIO` | 0.45 | Of the input budget: cut back to here |
| `CONTEXT_KEEP_RECENT_SEGMENTS` | 3 | Never curated |
| `CONTEXT_TOOL_RECORD_CHARS` | 400 | Survives in a compacted record |
| `CONTEXT_SUMMARY_TARGET_WORDS` | 250 | Target length of the note |
| `RECALL_MAX_MATCHES` / `RECALL_SNIPPET_CHARS` | 3 / 1500 | `recall_context` output bounds |

The ratios and char limits live in `workflow_backend/thresholds.py`; the two
model settings in `workflow_backend/settings/base.py`.

---

## 7. Tests

`chat/tests/test_curation.py`, grouped by the mistake each pins:

- **`OrphanedToolResultTests`** — trimming never leaves a result without its
  call, something is actually dropped (so the first assertion is not vacuous),
  and tool-call arguments are counted
- **`SegmentTests`** — a turn and its results group as one; two results for one
  turn stay together; an orphan attaches rather than standing alone
- **`CurateTests`** — compaction keeps the call and shrinks the result; the
  recent tail is untouched; indexing on archives and names the id, off admits
  the loss; a fold removes by id and leaves exactly one note; a previous note is
  absorbed; a failed fold changes nothing; a second pass does not re-archive
- **`RecallTests`** — another user's and another run's archives are unreachable
- **`NoteReachesTheWireTests`** — `to_wire` renders the note (it used to drop
  every `SystemMessage`, which would have made the fold a silent amnesia)
- **`CurateNodeTests`** — chat is untouched, the fold is charged to the run, and
  a broken curator does not fail the run

`chat/tests/test_curation_e2e.py` drives the **real graph** for twenty
tool-calling turns against a stub provider. Everything above passes with the
pieces wired to each other wrongly — a graph edge in the wrong place, a policy
that never reaches the node, state updates computed and dropped. This asserts on
what actually left for the provider: no request exceeds the budget, no request
orphans a `tool_call_id`, a fact planted in turn 2 is still reachable through
`recall_context` at turn 20, and a short run is untouched. One test deliberately
runs with curation off and asserts the same transcript *would* have overflowed,
so none of the others can pass vacuously.

`chat/tests/test_context_acquisition.py` covers the other half: the KB scope
(including that `read_document` cannot be used around it), the prompt carrying
ids and backends, the full `rag` grant, the delegation payload bounds, the
briefing, and the worker's read-through to its parent's archive.

Plus `agents/tests/test_agent_runtime.py::RetrievalToolAdvertisementTests` for
withheld-until-stored, and `GrantMappingTests` for why the retrieval pair is not
grant-gated.
