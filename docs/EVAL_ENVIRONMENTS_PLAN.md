# Evaluation environments — the plan

**Status:** E-1–E-5 built 2026-09-24; E-6 (paid proof runs) still needs the
user's go-ahead. Only Phase 0 existed before this change. Review fixes
2026-09-25: simulated tools obey the connector scope and per-tool deny,
account-touching tools are withheld (`notify_user` simulated), hidden KBs
never reach a picker, non-world cases run outside the world, sweeps pin
their world, blind answers match by id, generation runs in the background,
and worlds no longer require file access (see CLAUDE.md for the detail).
**Supersedes:** the "no fixtures" rule in `eval/generator.py` (see §1.3).
**Related:** `EVALUATION.md` (how eval works today), `EVALUATION_PRODUCTION_PLAN.md`
(the implemented production plan), `eval/workspace.py` (per-case folders, the seed
this grows from).

---

## 0. The problem in one paragraph

A new agent has nothing to work on. Its owner has no test files, no inbox with the
right emails in it, no knowledge base, no run history, and a research agent's "right
answer" changes every time the web does. So today the only cases anyone can write
for a new agent check *what it says*, never *what it does*, and the expected answer
is a guess somebody typed. The fix: **the judge builds a small fake world for the
agent, writes the questions about that world, and knows the right answers because it
made the world.** The agent runs inside that world — never against the owner's real
data — and is graded against the judge's answers.

## 0.1 Rules this plan keeps (our evaluation philosophy)

These are the rules the existing eval app already follows. Every phase below must keep
them.

1. **A score is provisional until a person has been asked.** Anything a model writes
   (a world, a case, an expected answer) is a **draft** until a person accepts it,
   **on the Evals page only**. Chat can make drafts; it can never accept them.
2. **The model under test never writes its own exam.** Worlds, cases and expected
   answers come from the judge model (`EVAL_JUDGE_MODEL`), which is deliberately a
   different model from the agent's (CLAUDE.md, "Model IDs").
3. **A judge is never alone.** Every case has at least one deterministic check next
   to any `llm_judge`.
4. **Every case proves it can pass and can fail.** A case the ideal answer fails, or
   the untouched world passes, is broken and is not saved (the work-tier rule, now
   applied to generated cases).
5. **One door.** Eval runs go through `run_agent` like every other run. The world
   changes *what the tools touch*, never *which code path runs*.
6. **Fail closed.** If a tool has no fake version, the agent does not get that tool
   in the eval. It is never allowed to reach the real service.
7. **The owner's real data never enters an eval.** Otherwise a score depends on
   what the owner's folders held that day, and their private files get sent to the
   judge.

---

## 1. Where we are

### 1.1 Built (Phase 0, 2026-09-24)

- **Eval mode.** `caller='eval'` never pauses. A call that would have needed approval
  is recorded as an *intent* and then run or blocked (`EvalSuite.gated_calls`).
- **`ask_user`.** Agents record questions as a tool call and carry on with a stated
  assumption. Graders: `asked_question`, `requested_approval`.
- **Drafts.** `eval/generator.py` drafts cases from the agent's configuration. Real
  runs import as cases. Both are `needs-review` drafts, accepted on `/evals`.

### 1.2 Already there to build on

- `eval/workspace.py`: a per-case folder of fixture files, **reset before every
  attempt**, snapshotted afterwards, and graded by the `file_*` / `csv_*` / `json_*` /
  office graders. This is the file slice of a world, working today.
- The work-tier rule: expected values come from a reference solution over the same
  fixtures, and a test fails if a case is unpassable, or passable with nothing done.
- `AgentToolbox.dispatch` (`agents/agent/runtime.py`): the one place every agent
  tool call goes through. That is where eval calls get sent to the fake world.

### 1.3 The gaps

| # | Gap | Consequence today |
|---|---|---|
| E1 | No fake world. The generator drops every fixture-based check on purpose | Generated cases can only check prose, so "does the job" is barely tested |
| E2 | The agent reads the **owner's real** files, knowledge bases and inbox during an eval | Scores depend on whatever the owner's folders held that day; private data goes to the judge; `read_all_write_own` agents see everything |
| E3 | "Record and run" hits **real** services | Evaluating a mailbox agent sends real email |
| E4 | Expected answers are prose rubrics a model guessed | The judge grades against a rubric, not a known answer |
| E5 | Web answers change over time | A research case that passed yesterday fails today with the agent unchanged |
| E6 | Chat can create **active** cases (`add_eval_case`) and `/eval` stores the assistant's answer as the task | A model's case reaches the score without review; `/eval` cases test the wrong thing |

---

## 2. The design

### 2.1 A world is data, owned by the suite

An **`EvalWorld`** is one fake situation (a small company, a project, a customer
base) that many cases share, just as a real agent works in one real situation. It
belongs to a **suite**, not a case: one world with 12 cases in it is cheaper to build,
more realistic, and lets cases refer to each other's data ("the duplicate invoice
from case 3").

```
EvalWorld (new model, one per suite, versioned)
  suite            FK EvalSuite
  version          int                 # a regenerated world is a new version; runs name the version they used
  status           draft | accepted    # accepted on /evals only
  brief            text                # "Acme Tools, 40 staff, Q3 close in progress" — shown to the reviewer
  surfaces         JSON                # which parts exist, see §2.2
  fixtures         JSON                # the data itself, per surface (size-capped)
  facts            JSON                # the ground truth the judge planted: {"q3_revenue": 412300, "duplicate_invoice": "INV-1043", ...}
  created_by_model str
  cost_usd         decimal
```

Cases keep their own per-case overrides in `input_data['__workspace__']` exactly as
today. A case may add files on top of the world; it never replaces it.

**`facts` is the key idea.** The judge writes the world *from* a list of facts ("Q3
revenue is 412,300; invoice INV-1043 appears twice; Priya's email asks to move the
review to Friday"). Every expected answer then points at a fact, so it is a lookup
into what the judge planted, not a second guess.

### 2.2 Surfaces: which parts of the world exist

A world holds only the surfaces this agent's grants can reach. Each surface needs
three things: **fixtures** (what exists at the start), a **simulator** (how the
agent's tools read and change it), and **state graders** (checks on what it looks
like afterwards).

| Surface | Agent grant | Fixtures | Simulator | New graders | Phase |
|---|---|---|---|---|---|
| Files | `fileOps`, `office`, `codeExecution` | files, including csv/xlsx/docx/pptx made by our own office renderers | **none needed**: real `vfs` rows in an eval-only folder (reuses `workspace.py`) | existing `file_*`, `csv_*`, `json_*`, office graders | E-1 |
| Knowledge base | `rag` | documents | a **hidden eval KB** indexed from the fixtures, dropped when the world changes | existing text graders + `cited(doc)` | E-2 |
| Mailbox | `mcp` → Gmail | messages, threads, labels | `sim/mail.py`: search/read/send/draft/label over fixture JSON; sends go to an **outbox** | `env_sent(to, contains)`, `env_not_sent(to)`, `env_label(...)` | E-3 |
| Calendar | `mcp` → Calendar | events, attendees | `sim/calendar.py`: list/create/update/delete | `env_event(title, start)`, `env_no_event(...)` | E-3 |
| Drive / Sheets | `mcp` → Drive, Sheets | files, sheet cells | `sim/drive.py` | `env_cell(...)`, `env_file(...)` | E-4 |
| Web | `webSearch` | a small **fixed corpus**: pages, plus the search results each query returns | `sim/web.py`: `web_search` / `read_url` answer from the corpus; an unknown query gets "no results" | existing text graders + `cited(url)` | E-4 |
| Messaging / SQL / HTTP APIs / browser | `talk`, data, API, browser grants | later | later | later | not in this plan; those tools are **withheld** in environment suites until they have a simulator (rule 6) |
| Sandbox | `codeExecution` | — | none needed: the sidecar has no network and sees only files passed in | — | as is |

### 2.3 How an eval run is pointed at the world

One new piece: `eval/environment.py::EvalEnvironment`, built by the runner for each
attempt and handed to `run_agent(..., environment=env)` (only when `caller='eval'`).
It does three things, each where the equivalent real thing happens today:

1. **Scope: what the agent can see.** `build_file_scope` returns a scope rooted at
   the world's folder, whatever the agent's `fileAccess` mode says. `kb_scope` is the
   world's hidden KB and nothing else. The connector scope is the simulated
   connectors only. This closes E2: the owner's real tree is unreachable during an
   eval, not merely unlikely to be read.
2. **Dispatch: where tool calls go.** `AgentToolbox.dispatch` checks
   `env.simulates(name)` first. A simulated tool is answered by the simulator. A real
   tool with no simulator is **withheld** from `descriptors` and refused at dispatch
   (rule 6), so it never reaches a real service.
3. **State: fresh every attempt, snapshot afterwards.** `env.prepare()` resets every
   surface (as `workspace.prepare` already does for files). `env.snapshot()` produces
   `GradeContext.env` (outbox, calendar, drive, and files as before) for the state
   graders.

Consequence for E3: in an environment suite, **"Record and run" is safe by
construction**, because "run" means "run against the simulator". The intent is still
recorded, so `requested_approval` works unchanged.

Simulators are **deterministic**: same world, same calls, same answers. They record
every call so the grader and the reviewer can see exactly what the agent did to the
world.

### 2.4 How a world and its cases are generated

A pipeline in `eval/generator.py`. Model steps are marked **[judge]**; everything
else is our code.

```
1. Profile     agent config → grants → which surfaces to build (§2.2)
2. Facts       [judge] a scenario + 10–20 planted facts, including traps
               (a duplicate, a conflicting date, an email containing an injected instruction)
3. World       [judge] fixtures that contain exactly those facts:
               files, mailbox, KB docs, web corpus
4. Check world our code: fixtures parse (CSV loads, dates are dates), size caps hold,
               each fact appears where the judge says it does (string/number search)
5. Cases       [judge] tasks over the world, each with:
               goal, category, the facts it depends on, an **expected answer**,
               expected state changes (e.g. "one email to priya@acme.test, no others"),
               and graders from the allow-list (now including file/env graders)
6. Solve       [judge, separate call] given only the world and the goal, answer it.
               If the solve disagrees with the expected answer, the case is
               ambiguous or wrong → dropped with that reason.
7. Prove       our code: every deterministic grader passes on the ideal outcome
               (the expected answer + expected state) and fails on the untouched
               world with an empty answer (rule 4). Fails → dropped.
8. Save        world (draft) + cases (drafts) → /evals for review
```

**The expected answer is the judge's, and only the judge's** (your decision,
2026-09-24). No agent run is ever used to set it. `EvalCase.reference` becomes the
expected answer, with the facts it rests on, instead of a vague rubric, and
`llm_judge` is told *"the correct answer is X because of facts F; score whether the
agent reached it"*. That is far more reliable than "is this a good answer?".

Step 6 is what makes "set by the judge" trustworthy. One judge call writes the
answer, a second call re-derives it blind, and only answers the two agree on survive.
It roughly doubles the cost of generation, not of every run.

### 2.5 Reviewing on the Evals page (the only place anything is accepted)

- **World card**: the brief, the planted facts, and a browser over the fixtures
  (files, inbox, events, KB documents, web pages). Accept or regenerate the world;
  a regenerated world is a new version and invalidates the cases built on the old one.
- **Case list**: each draft shows its goal, **expected answer**, the facts it relies
  on, expected state changes, and its checks. Accept, reject, or edit then accept.
- **Rules**: a case cannot be accepted before its world is. A sweep runs only
  accepted cases on an accepted world, and each `EvalRun` records the world version
  it ran on, so scores from different worlds are never compared as if they were the
  same.
- Results show the recorded intents (Phase 0) and, for environment suites, a
  **what changed** panel: emails sent, events created, files written.

### 2.6 What chat may do

Chat is how most people will start ("test my invoice agent"), but under rule 1 it
only ever produces drafts. The chat tools stay chat-only (`authoring.py`'s rule:
agents can never reach them, so no agent builds or runs evals on its own), and every
write needs approval.

| Tool | Approval | What it does |
|---|---|---|
| `list_eval_suites`, `get_scorecard` | — | exists |
| `create_eval_suite(agent_id, name)` | yes | empty suite pointed at an agent |
| `generate_eval_world(suite_id, focus, cases)` | yes (shows estimated judge cost) | runs §2.4; drafts only. The chat model passes `focus` ("month-end close with duplicate invoices"); the judge writes everything |
| `import_eval_cases_from_runs(suite_id, source)` | yes | drafts from real runs |
| `run_eval_suite(suite_id)` | yes (spends credits) | exists; refused if the suite has no accepted cases |
| `get_eval_results(run_id)` | — | per case: pass/fail, failing check, intents, what changed. So chat can explain *why* and propose an `update_agent` (which asks for approval separately) |

After generating, chat replies with a link to the suite on `/evals` ("12 drafts
waiting for your review"). There is no accept tool, by decision.

Fixes to existing paths (E6):
- `add_eval_case` saves a **draft**.
- `/eval` stores the user's question as the goal and the saved answer as the
  reference, as a draft.
- All five write paths go through one function, `eval/api.py::save_cases(...)`, so
  the draft rule lives in one place.

---

## 3. Phases

Each phase is shippable on its own and leaves every existing suite working. A suite
without a world behaves exactly as today.

### E-1 Files world (≈2 days) — the foundation
- `EvalWorld` model and migration; `EvalRun.world_version`.
- `eval/environment.py` with the files surface only (built on `workspace.py`), and
  **scope confinement** for files (closes E2 for files).
- `run_agent(environment=)` → `build_file_scope` override; withhold unsimulated tools
  in environment suites.
- Generator steps 1–8 for files; world + case drafts; allow-list gains the file and
  office graders.
- Evals page: world card (facts + file browser), expected answer on each case, the
  "accept the world before its cases" rule.
- **Done when:** generating a world for the gallery `analyst` template gives drafts
  that pass step 7; one accepted sweep runs with the owner's own files unreachable
  (a test asserts a `read_all_write_own` agent cannot list the owner's root during
  an eval).

### E-2 Knowledge base (≈1 day)
- Hidden eval KB per world version, excluded from every listing and from `kb_scope`
  outside eval; dropped when the world is regenerated or the suite deleted.
- Graders: `cited(doc)`.
- **Done when:** a `writer`-template world with policy documents grades a "which
  policy applies" case; the user's own KBs are unreachable during the run.

### E-3 Mailbox and calendar (≈2–3 days)
- `sim/mail.py` and `sim/calendar.py`, covering every native Gmail / Calendar tool
  in `chat/tools/google/`; a test fails if a native connector tool has no simulator
  (the same trick as the declared-knob test in `tools_config`).
- Graders: `env_sent`, `env_not_sent`, `env_event`, `env_no_event`.
- `GradeContext.env` + snapshot; the "what changed" panel.
- **Done when:** an inbox-triage agent at `ask` autonomy, under "Record and run",
  sends its reply to the simulated outbox (graded) and **no** real Gmail call is made
  (asserted with the Google client patched to fail if touched).

### E-4 Drive/Sheets and a frozen web (≈2 days)
- `sim/drive.py`; `sim/web.py` with a per-world corpus and fixed search results.
- **Done when:** a research case gives the same score on two runs a week apart.

### E-5 Chat front door (≈1 day)
- The §2.6 tools; `save_cases` as the one write path; `add_eval_case` and `/eval`
  produce drafts; chat links to `/evals` for review.
- **Done when:** "test my invoice agent" in chat produces a world and drafts, links
  to the page, and nothing is scored until accepted there.

### E-6 Proving it on real agents (paid, ask first)
- For each gallery specialist (analyst, slides, writer, reviewer): generate a world,
  review it, run 3× (pass^k as in the work tier), and record the cost of generation
  and of one sweep.
- **Done when:** a scorecard per specialist exists, and the cost per world and per
  sweep is written into this doc.

Order: E-1 → E-2 → E-3 → E-4, with E-5 at any point after E-1. E-6 needs the user's
go-ahead because it spends money.

---

## 4. Risks and how each is handled

| Risk | Handling |
|---|---|
| The judge plants a fact, then writes a fixture that contradicts it | Step 4 searches the fixtures for every fact; step 6 re-derives each answer blind |
| Worlds too small to be realistic, or too big to be cheap | Size caps per surface (files ≤ 30, mails ≤ 60, KB docs ≤ 20, pages ≤ 25); a `focus` field to aim the world |
| Simulators drift from the real tools' behaviour | Simulators return the **same result shapes** as the real tools, pinned by a shape test per tool; the agent must not be able to tell |
| An agent is tuned to one world | "Regenerate world" makes a fresh version; the scorecard shows the world version, and a real regression shows up on both old and new worlds |
| Cost | Generation ≈ 3–4 judge calls per world (facts, world, cases, solves batched); runs cost what they cost today. Shown on the approval card and the world card |
| Hidden eval data leaking into the real product | Eval folders and KBs are flagged, excluded by the managers the listings already use (the `LiveManager` pattern), and cleaned up on regeneration and suite deletion |

## 5. Open decisions (defaults proposed; confirm with the user)

1. **One world per suite** (default) or one world per case?
2. **Web in evals**: frozen corpus (default; stable scores) or live web with the
   judge re-checking facts at grading time?
3. **Where eval files live**: a hidden `/.eval/` tree the file browser never shows
   (default), or a visible `/Evals/<suite>/` the owner can open?
4. **Worlds for existing agents too**: should an agent that *does* have real data
   still be evaluated in a generated world by default? Proposed yes: real data stays
   for "From runs" cases only.
5. **Regenerating an accepted world**: keep the old version's cases, marked as
   belonging to the old world (default), or discard them?
