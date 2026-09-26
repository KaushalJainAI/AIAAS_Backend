# Slash commands: a simple guide

A slash command is a short cut you type in the chat box. It starts with `/`.

It is structured input, not a prompt trick. The app reads it, checks it, then runs it. The model never has to guess what you meant.

Example:

```
/agent Reporter summarise my inbox
```

This says: which thing (`Reporter`), and what to do (`summarise my inbox`). The app checks that `Reporter` is really one of your agents before anything runs.

---

## 1. How to use one

1. Type `/` at the start of the box. The palette opens.
2. Keep typing to filter. Pick with mouse, Enter, or Tab. Esc closes.
3. Fill the arguments. For people, files, agents, models, connections, a picker appears. A picked item becomes a chip. A chip carries the id, so the name is never guessed twice.
4. Press Enter to send.

A `/` in the middle of a sentence is plain text. `/Chat/notes.md` never opens the palette. Only a leading `/` is a command.

You can also type the full line by hand, without the palette:

```
/goal launch the newsletter in 7 days
```

The backend parses it the same way.

---

## 2. The three kinds

There are three kinds. The palette shows which is which.

**Client — runs in your browser.** No server call. A setting or a panel.

- `/new`, `/mode`, `/model`, `/effort`, `/help`

**Action — runs once on the server, returns a card.** No model call. Fast.

- `/cost`, `/status`, `/memory`, `/file`, `/export`, `/help`
- Sent as `POST /api/chat/commands/run/`. Turn and client commands are refused there with the reason.

**Turn — starts a normal chat turn with extra instructions.** The model still answers.

- `/agent`, `/goal`, `/research`, `/search`, `/read`, `/code-review`, `/deck`, `/doc`, `/sheet`, `/chart`, `/image`, `/kb`, `/skill`, `/schedule`, `/publish`, and more.
- These run through the normal message endpoints, not `/run/`.

If you type a client command as a message, the backend tells you to pick it from the menu. It never runs in the model loop.

---

## 3. What happens after you press Enter

This is the pipeline, step by step.

```
You send: /agent Reporter summarise inbox
    |
    v
1. Read the line
   Split into name + rest. "/agent" + "Reporter summarise inbox".
    |
    v
2. Find the command
   Look up "agent" in the registry, including aliases.
   Unknown name? Stop here. Suggest the closest real name.
   Example: "/agnt" -> "Did you mean /agent?"
    |
    v
3. Check access
   Guests see only guest commands.
   Hidden requirements stay hidden (no workspace engine = no /code, no browser engine = no /browse).
   Both doors are checked: hidden in the list AND refused if typed.
    |
    v
4. Resolve each argument
   Chips pass through (already checked).
   Names resolve case-insensitively against your own rows only.
   A name that resolves to nothing is refused here.
   Example: "/agent Reprter hi" -> "No agent called 'Reprter'".
   It never runs some other agent with mangled text.
    |
    v
5. Run the handler
   Each command owns one function: its listing, its picker, and its check in one place.
   Result is one of three:
   - ok: ready to go
   - confirm: needs your press on a sheet first (see section 5)
   - error: refused, with a message written for you
    |
    v
6a. Action -> return the card
    The card renders in the transcript (mission progress, cost, findings).
    Done. No model call.
    |
6b. Turn -> join the chat turn
    The resolved command becomes a trailing context message for this turn.
    It never goes in the system prompt (that would break caching).
    It may also pin an intent or narrow the toolbox (e.g. /deck pins office tools).
    The turn then runs as normal: model, tools, answer.
```

Failures show under the input, with your text left in place. A failed command is never sent to the model as plain chat. A silently un-run command would read as one that ran.

The resolved command is stored on your message (`metadata.command`). The transcript shows a short chip like `/agent Reporter`. Regenerate replays the same command.

---

## 4. The full list (by group)

Groups are what the palette shows.

**Agents**

- `/agent <name> <task>` — hand a task to one of your saved agents. Delegates, never hands off. The agent runs with its own model, tools, and limits. Its answer comes back as a run card, then as a message in this chat. The chat model then uses that answer. Refused in `plan` mode (starting a run is not a read).

**Goals (missions)**

- `/goal <text>` — start a long-horizon mission. Needs confirm sheet (budget, deadline). Only the model could start missions before this route existed.

**Memory**

- `/memory <text>` — save a durable fact about you. `/memory_forget` — remove one (confirm sheet). Facts render in the system prompt. Agents can read but never write them.

**Code**

- `/code <project> <task>` — work inside a code project (needs workspace engine). `/code-review` — run the reviewer template in read-only mode, returns a `findings` card.

**Browser**

- `/browse`, `/search`, `/read`, `/download` — search the web, read a page, fetch a URL as your file. Composed URLs are checked before fetch (see injection notes in code).

**Data and files**

- `/file`, `/kb`, `/extract`, `/sql`, `/api`, `/sheet`, `/chart`, `/dashboard`, `/diagram`, `/pdf`, `/export`, `/connect` — find files, search a knowledge base, pull structured rows, run a scoped query, draw from data, export a file type.

**Documents (office)**

- `/deck`, `/doc`, `/sheet`, `/pdf`, `/diagram` — describe slides, sheets, or blocks. Our code renders the real `.pptx` / `.xlsx` / `.docx`. The model never writes markup. Limits refuse rather than shrink (a 9th chart series is refused, not dropped).

**Media**

- `/image` — make an image (asks first: it spends money). `/speak`, `/transcribe` — voice in and out (hidden with no speech engine). Audio and video generation stay off the tool surface (too costly per call).

**Session**

- `/help` — list what you can run. `/new` (alias `/clear`) — fresh chat. `/mode` — Ask / Auto / Plan. `/model`, `/effort` — picker and reasoning level. `/skill` — attach a skill. `/research` — deep research first. `/status`, `/pause`, `/resume`, `/approvals` — what is running, stop the world, resume it, see pending approvals.
- `/message`, `/sign`, `/publish`, `/run` — messaging, e-sign, publish a page, run things (each gated by its own scope and confirm where needed).
- `/eval` — make or run an evaluation case for an agent.

Unknown names get a suggestion, never a guess. `/reserach x` is not stripped to `x`. It is an error with a hint.

---

## 5. Confirm sheets: the second tap

Some first actions spend money or leave the platform. They do nothing until you press Start on a sheet that shows the resolved arguments.

- `/goal` — starts a mission (budget, deadline, agent).
- `/schedule` — arms a cron trigger (expression, timezone).
- `/publish` — makes a page visible by link or publicly.
- `/code-review` — states it runs read-only before it starts.
- `/image` — states the per-call charge.

Pressing Start is the approval. Everything after that is gated as usual: every tool call the model or the started agent then makes still meets autonomy, grants, scopes, and per-tool permissions.

---

## 6. Rules worth knowing

**Consent covers the first action only.** Typing `/agent Reporter ...` starts the run without the usual `run_agent` approval card, just like the Run button. It covers nothing after that. The worker's tool calls are gated normally.

**Plan mode withholds.** `/agent` refuses in `plan` mode. Plan removes mutating tools, and starting a run is not a read. Switch to Ask or Auto first.

**Empty means default, not broken.** A fresh install lists every command with no setup. An empty agent or connection scope means unrestricted (agents predate the field). No silent narrowing.

**Completion and validation share one predicate.** The picker offers only what the validator accepts. A name the completer never offered is not one that can run.

**One line, 2000 chars.** A command is one line. Longer pasted prose starting with `/` is refused as too long to be a command.

**Aliases work everywhere.** `/clear` runs `/new`. Resolution, completion, and help all know the aliases.

---

## 7. Two walks

**Hand work to an agent:**

```
/agent Reporter summarise my inbox from today
```

1. `Reporter` resolves to your agent id (case-insensitive, yours only).
2. Preflight checks the agent's model key and guardrails before anything streams.
3. A context block joins this turn: "the user handed this to Reporter, use its answer, do not redo the work".
4. The run starts through the one door (`caller='chat'`). Status, todos, and files stream as `agent_run` frames.
5. The answer lands as a run card, then as a message from Reporter. The chat model can chart it, file it, or quote it.

With `with_context`, the last few turns ride along as background, capped by the delegation payload limits, marked as context not instructions.

**Start a mission with consent:**

```
/goal get 20 beta users in 2 weeks
```

1. Resolves to `confirm` with a sheet: goal text, agent picker, budget, deadline.
2. You press Start. `POST /api/chat/commands/confirm/` runs the same service the model uses.
3. The mission row exists. Pause, resume, and cancel live on `/missions` and as commands.

---

## 8. For developers: where it lives

- `Backend/chat/commands/registry.py` — `@command`, `Arg`, the three kinds, the catalogue.
- `Backend/chat/commands/resolve.py` — `split_leading_command`, `complete_arg`, `resolve_line`, `CommandError`. Parsing, picking, and validation in one module so they cannot disagree.
- `Backend/chat/commands/views.py` — `GET /api/chat/commands/`, `GET .../complete/`, `POST .../run/`, `POST .../confirm/`, plus the `/api/missions/` routes. Importing it registers every domain module.
- `Backend/chat/commands/<domain>.py` — one file per topic (`agents`, `missions`, `memory`, `review`, `session`, `library`, `shortcuts`, `web`, `media`, `knowledge`). Add a command by adding one block.
- `Backend/chat/turn/pipeline.py` — `TurnRequest.command`, `_resolve_command`, `_command_toolbox`, `_start_command_run`. The turn path.
- Frontend: `src/lib/commands.ts` (pure parse, rank, chips), `src/api/commands.ts` (transport), `src/hooks/useCommands.ts` (one catalogue per session), `src/components/chat/CommandPalette.tsx` + `CommandCard.tsx` (palette and cards).
- Tests: `Backend/chat/tests/test_commands.py`, `src/lib/__tests__/commands.test.ts`.

One copy only. A frontend template would drift from the tools, grants, and contracts it depends on, bypass validation, and leave other clients with nothing. So the registry is backend code. The frontend ranks and renders; the server decides.
