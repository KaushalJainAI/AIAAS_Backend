# Agent configuration & Settings — gap audit and improvement plan

Audited 2026-09-18 across the agent builder, `AgentSerializer`, the builder chat,
the agent runtime, the schedule sweep, the Settings page and the chat cost chip.
Every finding below was traced in code; nothing was reproduced in a live browser.

The theme running through almost all of it: **a control that is stored, shown
and never read**, or **a number shown without saying what it is**. Both look like
working features, which is why they survived.

---

## Done in this change

### The conversation cost chip (`₹1.02` in the chat header)

How it works: each chat turn is priced once, from the summed token usage of
every model call in the turn's loop (`chat/turn/pipeline.py::_persist_answer` →
`llm/pricing.py::cost_for_usage`). If OpenRouter reported what it charged, the
figure is `billed`; otherwise it is `estimated` from the `AIModel` price table;
with neither it is `unpriced` and shown as `—`. The session keeps a running sum.
It is the **model provider's price**. It is not credits: credits
(`llm/credits.py`) are a separate allowance, charged only when the *platform's*
key pays, at 1 credit per 1,000 tokens, never on free models.

What was wrong, and is fixed:

| Fault | Effect | Fix |
|---|---|---|
| `TokenUsage.__add__` kept a reported cost when another call in the same sum reported none | A partly reported turn was stored as a confident, **understated `billed`** figure; `combine_sources` never saw the parts | A side with tokens and no cost voids the reported total, so the whole turn is estimated from the price table (`llm/usage.py::_combined_cost`) |
| The follow-up-questions call (one per answer, on the user's own model) was never counted | Every conversation total left out a call per turn | Its usage is returned via `usage_sink` and priced into the turn (`chat/turn/agent.py::suggest_follow_ups`) |
| The chip showed a bare `₹1.02` | Read as "this chat charged me ₹1.02" whatever it was | Shows `charged` / `est.`, and the tooltip says what is counted and whose money it is (`lib/cost.ts::describeConversationCost`) |

The vision witness (`ask_vision`), a separate model called inside a tool,
was the last uncounted call; it is counted since item 5 below.

### Security

- **`credits_remaining` was writable through `PATCH /api/auth/profile/`**. Any
  user could give themselves an unlimited platform-key allowance. Now
  read-only, along with `llm_credential_id`.
- The same endpoint let a user take an email address another account already
  held. Sign-in and password reset look accounts up by email. Now refused.

### Agent status is enforced

`paused` promised "schedules stop firing and no agent may delegate to it". Nothing
checked it. Now `runtime._check_status` refuses unattended callers (schedule,
webhook, delegation) with `AgentPaused`, and the sweep **skips** the slot with
outcome `paused` instead of counting a failure. A refusal would have disabled
the schedule for good after five slots. The owner can still run it by hand.

### Settings are stored *and used*

| Setting | Before | Now |
|---|---|---|
| Timezone | Read only by the email digest; 5 options; unvalidated | Chat's clock, an agent's `useEnvironment` time, the builder chat's scheduling zone, new-schedule default; every IANA zone; validated; offers the device's zone when still `UTC` |
| Language | Stored as a name while the column defaulted to a code; read by nothing | Normalised to a code; the model is told to reply in it (yielding to the language the user actually writes in) |
| Display name | Stored, not editable, not read | Editable; the assistant and agents know what to call the user |
| Bio | Stored, not read | Read by chat and agents (cut to 600 chars on a word) |
| Default temperature | Stored, no control, not read | Control on General; seeds new agents |
| Instance name | Stored, shown nowhere | Sidebar brand when customised |
| Webhook URL (API tab) | Showed `/api/webhook/`, a route that does not exist | Points to per-agent webhooks on Schedules |
| Cancel button | No handler | "Discard changes" resets the form |

One module does the rendering: `core/preferences.py`. What it adds to the system
prompt only changes when Settings is saved, so the cached prompt prefix still
holds (same rule as `core.memory`).

### Builder and runs

- **Run** button on the builder and on every agent card (`RunAgentDialog`). The
  execute endpoint had no caller in the app before this.
- `/runs?run=<id>` opens that run, including one the current filter hides, and
  keeps refreshing it while it's live. This also fixes the existing "Open that
  run" link on delegated runs, which went nowhere.
- Runs waiting for approval show **Needs you**, not "Queued".
- Builder chat no longer offers `tools.shell` (unserved) or `reviewAgent`
  (unread), no longer asks for the retired `trigger` field, and is given the
  user's timezone.
- `shell` shows as "Not available yet" on the board (can only be turned off).
- The delegation picker says which agents would refuse (paused, or not cleared
  to run on their own).
- Unsaved-changes guard on the builder; after a save the board takes the
  server's normalised copy, so it doesn't read as unsaved for ever.
- Temperature slider matches the backend range (0–2). (Delete now keeps the
  agent's runs — item 1 below — and its confirmation says so.)

Tests: `llm/tests/test_usage.py`, `chat/tests/test_conversation_cost.py`,
`agents/tests/test_agent_status.py`, `agents/tests/test_builder.py`,
`core/tests/test_preferences.py`, `src/lib/__tests__/cost.test.ts`.

---

## The plan, executed (2026-09-18)

All 23 items were worked through on the day. Each line says what was done,
or, where the plan left a choice, what was decided and why. Tests are named
so each claim can be checked.

### P1: trust and correctness

| # | Item | Outcome |
|---|---|---|
| 1 | Keep runs when an agent is deleted | **Done.** `ExecutionLog.subagent` and `SubAgentRevision.subagent` are `SET_NULL` (`logs.0019`); a deleted agent's runs are listed as "<name> (deleted)" from their revision snapshot. `archived` is settable and restorable (Agents → Archived), and the builder offers Archive before Delete. `agents/tests/test_agent_lifecycle.py` |
| 2 | Stop a run | **Done.** `POST orchestrator/runs/<id>/cancel/` → `runtime.cancel_agent_run`: cancels a live task by name, closes an approval-paused run directly, refuses (409) a delegated worker or a run in another process. Stop button on Runs. Also fixed: cancelled/timed-out/failed runs recorded **0 tokens**, which made them free to the spend cap; they now keep their turns' total. `agents/tests/test_run_stop.py` |
| 3 | Wire the run controls | **Done.** `RunControls` on a live run in Runs: Stop, a steer box and the autonomy switch, warning if the instruction landed on a newer run of the agent. Approve/reject stay in the Inbox (Overview) — one answering screen — and a paused run links there. Dead `agentsService.approve` removed |
| 4 | Say whose key paid | **Done.** `llm.access.payer` (same order as `preflight`) → `ChatMessage.paid_by`, `ChatSession.paid_by` (`mixed` once a conversation switches). The chip's tooltip names the payer. `chat/tests/test_conversation_cost.py::PayerTests` |
| 5 | Vision witness in chat cost | **Done.** `chat/turn/side_calls.py` — a context-scoped list the witness records into, priced per model into the turn. `SideCallCostTests` |
| 6 | Insights left out chat | **Done.** `cost_breakdown` adds `chat`, `all_cost_usd`/`all_cost_source`, `total_cost_source`; `by_workflow` says `billed` when it was. Settings → Billing shows 30-day spend (agents + chat) beside credits. `logs/tests/test_cost.py::InsightsSpendTests` |
| 7 | Verify an email change | **Done.** `auth/email/change/request/` + `confirm/`: code to the new address, current password where one exists. The profile PATCH refuses an email change. Also closed: **registration accepted an email another account held**. `core/tests/test_preferences.py::EmailChangeTests` |
| 8 | What `draft` means | **Decided: a label, not a switch.** 42 of 55 agents in the dev database are `draft` only because it is the default; enforcing it would silently stop most of them (and an unknown number in production). The copy now says so. Revisit together with a migration that promotes drafts which have schedules |

### P2: capability

| # | Item | Outcome |
|---|---|---|
| 9 | Per-tool connector picking | **Done.** "Chosen tools" mode with `ConnectorToolPicker`, fed by `mcp/servers/<id>/tools/` (native connectors answer from the registry). Names no longer offered are kept and marked, not silently dropped |
| 10 | Restore a revision | **Done.** `POST orchestrator/agents/<id>/revisions/<n>/restore/` — a save of the snapshot through `AgentSerializer`, recorded as a new `restore` revision; a since-deleted skill or connection is the serializer's 400, never a re-grant. Restore buttons in the builder and on `/agents/:id/history` |
| 11 | Duplicate; keep the builder chat | **Done.** Duplicate (no schedule, starts as draft). Builder chat kept per agent (`configure/` with `agent_id` → `agents/<id>/builder-chat/`, newest 40) |
| 12 | Runs filters | **Done.** "Needs you" (paused) filter, `?agent=<id>` filter, and a Runs link from the builder |
| 13 | Live progress | **Done.** `useLiveRun` listens on `ws/execution/<id>/` and refetches the detail (coalesced); polling drops to a 30 s safety net while the socket is up |
| 14 | Validate the model at save | **Done.** Unknown ids refused where the provider's catalogue is known (naming the provider an id actually belongs to); retired ones save but come back as `model_status: retired`, shown in the builder. Not policed for a provider with no rows, so a fresh install works |
| 15 | Summarising-model picker | **Done.** Offers only models the account can run (`available`, which the API sent all along); a saved choice that became unrunnable stays visible, marked "no key" |
| 16 | Test from the builder | **Done.** Evaluation section on the builder from the (previously uncalled) scorecard endpoint: latest settled score per suite, the revision it was scored under, results awaiting review, and a Run button |

### P3: leftovers

| # | Item | Outcome |
|---|---|---|
| 17 | `reviewAgent` | **Retired.** Off the wire, the builder types and publishing; `logs.revisions.RETIRED_KEYS` stops old snapshots registering it as a change |
| 18 | `default_max_tokens` | **Retired.** Read-only on the API, no longer sent by Settings |
| 19 | `llm_credential_id` + `settings/update/` | **Deleted** the route, `agents/views/system.py`, and the assistant's credential sync — nothing consumed it (BrowserOS included). The column stays, read-only |
| 20 | Theme restore | **Done.** `ThemeSync` applies the account's theme/accent on a browser with none of its own; a browser's own choice still wins |
| 21 | `SHAREABLE_KEYS` | **Done, and wider than planned.** Six retired fields out; `effort`, `outputContract`, `fanoutParallel`, `description`, `tags` in — a published agent installed without its result contract or effort was a different agent from the one published |
| 22 | Delete confirmation | **Done.** `ConfirmDialog`, worded for what delete now does |
| 23 | KB picker | **Left** — one implicit knowledge base per user; revisit if that changes |

### Still open

- A `draft` switch (item 8) needs a data migration first.
- `ChatMessage` token columns hold the answer's usage while `cost_usd`
  includes the follow-ups and witness calls; the per-message tooltip should
  eventually break those out.
- Verified by tests, type checks and lint only — not yet clicked through in a
  browser.
