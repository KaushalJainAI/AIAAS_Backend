# Agent templates

Shipping and sharing **autonomous agents** instead of DAGs.

Written 2026-07-29. **Phase 1 is built** (see §8); phases 2–4 remain design.

---

## 1. What changes, and why

Today a template is a workflow: `nodes` + `edges`. You publish the graph, someone
forks it, and it runs the same steps every time.

An agent is a different kind of thing:

| | DAG template | Agent template |
|---|---|---|
| What it says | "Do these steps, in this order" | "Here's the goal and your tools — go" |
| Steps known in advance | Yes | No, not even to the author |
| How you check it's safe | Read the graph | You can't. Only the guardrails constrain it |
| Fails when | Input shape changes | It's wrong ~7% of the time |
| Sells on | What it does | How often it's right |

The reason to make the switch is the third row of a real invoice pile: fifty
vendors, fifty layouts. A DAG breaks on the fifty-first. An agent works it out.

The reason it's harder is the fourth row. **You cannot read an agent to know it's
safe**, so the permission envelope stops being a nicety and becomes the entire
safety mechanism. That single fact drives most of the design below.

### DAGs don't go away — they get demoted

The intended end state is not "agents replace workflows". It's:

- **The agent is the brain.** Reads the messy input, decides what matters.
- **The workflow is the hands.** "Append these rows." "Send this mail." Fixed,
  deterministic, testable.

Agents call workflows as tools. Reliability lives where it touches the outside
world; flexibility lives where the mess is. The existing compiler and executor
become the safe execution layer rather than the product.

---

## 2. What already exists

Checked against the code on 2026-07-29, not assumed.

**Reusable as-is**

- `templates` app: publish, rate, comment, bookmark, fork, featured, `usage_count`,
  `fork_count`, `embedding` for similarity. All of the marketplace machinery.
- `orchestrator.Workflow` already carries most of an agent:
  `context` (TextField) · `supervision_level` · `llm_provider` / `llm_model` /
  `llm_credential` · `workflow_settings` (JSONField) · `is_template` · `tags`
- `WorkflowTestResult` already links to `workflow_template` — the seed of
  shipping eval results alongside a template.
- `HITLRequest` + the Inbox screen: the approval gate an agent needs.
- `chat.tools`: web search, deep research, scrape, `read_url`, KB search.
  **Corrected 2026-08-12:** this originally also listed file ops, a Python
  sandbox and `list_workflows` / `run_workflow`. Those were removed from chat
  deliberately and `chat/tests/test_rework.py::RemovedCapabilityTests` asserts they
  are neither advertised nor dispatchable. They are not reusable; re-adding any
  of them is a decision to reverse, not a wiring job.
- `sandbox.safe_execution`: the sandbox the Code node
  uses. This *is* reusable, and is what the agent runtime's `execute_python`
  wraps.
- Frontend: the agent builder at `/agents/new` produces an `AgentConfig` already.

**Missing** *(as of the original writing — the first three are now done)*

- ~~No agents API.~~ Built: `agents/views/agents.py`.
- ~~No tool-grant model, no egress policy, no spend cap.~~ Built as columns on
  `Workflow`.
- `WorkflowTemplate` stores `nodes`/`edges` — no agent shape. Still true.
- Portable `requirements` has a column but nothing writes it yet — it only
  matters at publish time (Phase 2).
- No permissions screen on install.
- No agent runtime, so a saved agent cannot yet be executed.
- Agents can't call workflows as tools yet (`run_workflow` exists in chat, not
  wired to an agent runtime).

---

## 3. Data model decision

**An agent is a `Workflow` with empty `nodes`/`edges`.**

This was the cheap-path hypothesis and it holds. `Workflow` already has the
brief (`context`), the model settings, the supervision level, and a JSON bag for
everything else. Reusing it means publish / fork / version / rate / clone all
work on day one.

Add to `Workflow`:

```python
kind = models.CharField(max_length=12, choices=[("workflow", ...), ("agent", ...)],
                        default="workflow", db_index=True)
tool_grants   = models.JSONField(default=dict)   # {"codeExecution": true, "shell": false, ...}
requirements  = models.JSONField(default=list)   # portable, see §4
guardrails    = models.JSONField(default=dict)   # egress, spend cap, autonomy, review agent
trigger       = models.JSONField(default=dict)   # {"mode": "maintenance", "cron": "0 9 * * 1"}
```

Use an explicit `kind` rather than inferring from `len(nodes) == 0` — an agent
that happens to have a cached sub-graph shouldn't silently become a workflow.

Mirror `kind`, `tool_grants`, `requirements`, `guardrails` onto `WorkflowTemplate`.

**Rejected: a separate `Agent` model.** It would need its own publish, fork,
version, rating and clone paths — a duplicate of `templates` for no gain. Revisit
only if agents grow fields that make no sense on a workflow.

---

## 4. The portability problem

An agent config points at rows that only exist in the author's account:

```json
{ "knowledgeBases": [1, 2], "connectors": ["gmail"], "skills": ["sk1", "sk4"] }
```

Installed elsewhere those IDs are broken, or worse, silently point at someone
else's row 1.

**A template stores requirements, not references.**

```json
"requirements": [
  { "id": "mailbox",  "type": "connector",      "provider": "gmail",
    "label": "Mailbox to read invoices from" },
  { "id": "vendors",  "type": "knowledge_base",
    "label": "Vendor records to reconcile against" },
  { "id": "gstin",    "type": "skill", "suggests": "GSTIN validation",
    "label": "GSTIN checking rules", "optional": true }
]
```

Install becomes: satisfy each requirement with something you own, via two or
three dropdowns. The resolved mapping is stored on the installed agent, not on
the template.

**Credentials never travel.** The template names the *kind* of connection; the
installer supplies their own. This is what makes sharing safe at all, and it
falls out of the design rather than needing a rule.

---

## 5. The permissions screen

The trust mechanism, and the part worth doing properly.

Installing a stranger's agent means letting a recipe you can't read touch your
email and act on your behalf. So install looks like an app store permission
prompt, generated from `tool_grants` + `requirements` + `guardrails`:

```
Finance agent  ·  by Kaushal Jain  ·  4.6 (28)

This agent will be able to:
  Read your Gmail  → [Work mailbox  ▾]
  ▤  Read and write Google Sheets           → [Payables 2026     ▾]
  Run Python in a sandbox (no network)
  ▦  Search a knowledge base                → [Vendor records    ▾]

Limits:
  Asks before sending anything
  No internet access from its sandbox
  ₹  Spends at most ₹500/month

From 412 runs by others:
  93% completed without needing anyone
  ₹4.20 average per run
  8% rejected at the approval step

                              [ Install ]  [ Cancel ]
```

Two rules:

1. **Every line is enforced, not described.** The screen renders from the same
   fields the runtime reads. If they can drift, the screen is a lie.
2. **Any change to grants on update requires re-consent.** Silently widening
   permissions on a background update is the one unforgivable failure here.

---

## 6. Trust signals

Stars are weak for agents. The useful signals already exist in `ExecutionLog`:

| Signal | Source | Why it matters |
|---|---|---|
| % completed unattended | runs with no `HITLRequest` | Did it save work or just nag? |
| Average cost per run | `credits_used` | What will this cost me? |
| Rejection rate at approval | HITL responses with `action="reject"` | **The honest one.** An agent people keep saying no to is a bad agent whatever its rating. |
| Eval pass rate | `WorkflowTestResult` | Does it work on cases it hasn't seen? |

Aggregate across installs, not just the author's own runs — an author's numbers
on their own data are marketing, not evidence.

---

## 7. Agents calling workflows

**Corrected 2026-08-12:** `_run_workflow` no longer exists — see
§2. This needs writing fresh against `executor/`, then gating by a new
`tool_grants.workflows` key, with the allowed workflow IDs listed
in `requirements` so they show on the permissions screen.

This is what keeps side effects deterministic: the agent decides *whether* to
send the reminder; a workflow decides *how* the mail is composed and sent.

---

## 8. Phases

**Phase 1 — make agents real (backend, blocking)** — *mostly done*
- `kind`, `tool_grants`, `requirements`, `guardrails`, `trigger` on `Workflow`,
  plus `sandbox` and `agent_context` (migration `orchestrator/0011`)
- CRUD at `/api/orchestrator/agents/`; `/agents/new` saves. See
  `agents/views/agents.py` and `agents/tests/test_agents.py`
- The egress knob §9.1 asked for. `shell + egress=full` is refused outright
- Agent runtime: [agents/agent/runtime.py](../agents/agent/runtime.py),
  run via `POST /api/orchestrator/agents/{id}/execute/`. Phase 1 is complete;
  `runs` counts are real.

Three decisions worth recording from building it:

- **It borrows `chat.agent`'s loop rather than forking it.** `TurnContext` grew
  three optional hooks — `tool_source`, `tool_dispatch`, `sensitive_tools` — and
  the runtime supplies all three. Chat turns pass none and are unchanged. A
  second loop would have meant two implementations of message threading that
  must agree, which is the thing the chat rewrite existed to fix.
- **Grants are enforced at call time, not only at advertising time.**
  `AgentToolbox.dispatch` re-checks the grant before running anything. A model
  can name a tool it was never offered, and "we didn't mention it" is not access
  control. §10's first risk is tested directly.
- **`shell` is refused, not faked.** It has no implementation this runtime will
  serve (§9.1). The run reports it in `unserved_grants` and the system prompt
  names it, so a configured-but-unavailable capability is visible rather than
  showing up as an agent that mysteriously cannot do its job.
- **`fileOps` used to be refused alongside it, and is now served.** What it
  unlocks is not the capability that was removed from chat: `inference/vfs.py`
  addresses rows in the user's own `Folder`/`Document` tree and cannot name a
  path on any disk. It takes two switches — the grant, plus
  `sandbox['fileAccess']` deciding *which* files — and with `fileAccess='none'`
  the tools are withheld rather than offered to refuse.
- **`mcp` is a new grant key**, which is what finally makes the MCP client
  reachable from an agent rather than from chat alone. Off by default: those
  tools reach real systems under the user's own credentials.

Two decisions worth recording, because they were not in the original design:

- **`PATCH` merges onto the stored config rather than replacing it.** A partial
  save that reset unsent knobs to their defaults would silently widen or narrow
  a grant — the failure §5 calls unforgivable. Tested.
- **Tool grants are stored as the full closed set,** not just the keys that were
  sent. An absent key has to read as "denied", never as "unset, so whatever the
  runtime defaults to".

**Phase 2 — sharing**
- Mirror the fields onto `WorkflowTemplate`; publish from an agent
- Install flow: requirement mapping + permissions screen
- Pin version at install; notify on update; re-consent when grants widen

**Phase 3 — trust**
- Aggregate the four signals across installs, show on template cards
- Ship eval results with templates via `WorkflowTestResult`

**Phase 4 — depth**
- Agents calling workflows as tools
- Review agent (second agent grades output before you see it)
- Recursive context management: compaction + indexing for long runs

The frontend for Phase 2 can be built against sample data before Phase 1 lands —
the permissions screen is where this idea either feels trustworthy or doesn't,
and it's cheapest to iterate on while it's still just a screen.

---

## 9. Open questions

1. **Sandbox for shared agents.** Today `_execute_shell` has no sandbox — env
   stripping and a 30s timeout, running as the app user with full filesystem
   read. Acceptable for your own agents; **not acceptable for a stranger's**.
   Either exclude `shell` from shareable grants, or sandbox it first
   (bubblewrap under `systemd-run`; this box has user namespaces and cgroup v2
   but no `/dev/kvm`, so microVMs are out).
2. **Model portability.** An agent tuned on Opus 5 may behave badly on a cheap
   model, and the buyer will blame the author. Templates should declare what
   they were tested on and a minimum tier — enforced or warned?
3. **Paid templates?** Ratings and forks exist; payments don't. Free-with-
   attribution first is the smaller step.
4. **Support burden.** "It did something odd" is unfalsifiable without the trace.
   Runs already captures it — does the author get to see an installer's traces?
   Useful for support, uncomfortable for privacy.

---

## 10. Risks

- **The permissions screen drifts from enforcement.** Mitigate by rendering it
  from the same fields the runtime reads, and testing that a denied grant
  actually fails at call time.
- **Non-determinism reads as brokenness.** Two installs of the same agent give
  different results. Set the expectation in the product copy: a capable
  assistant, not a machine.
- **A popular agent with a bad grant.** One widely-installed agent asking for
  more than it needs teaches everyone to click through the screen. Review
  featured templates by hand; default new grants to the narrow end.
- **Reusing `Workflow` couples two products.** If agents diverge hard, the shared
  table becomes a tax. Accepted deliberately — the alternative duplicates the
  entire marketplace up front.
