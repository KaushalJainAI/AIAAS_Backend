# Webhook triggers — plan review and what shipped

> Reviewed and implemented 2026-09-04. Supersedes `AGENT_BLOCKS_PLAN.md` §4's
> webhook paragraph, which describes a receiver that no longer exists.

## 1. What the plan said, and where it is wrong

`AGENT_BLOCKS_PLAN.md` §4:

> **Webhook** — a per-agent URL. `api/webhooks/<user_id>/<path>` already
> exists for workflows; agents reuse the receiver rather than growing a second.

Three things in that sentence are now false, and the corrections are the plan:

1. **The workflow receiver is gone.** It was deleted with the DAG product
   (`WORKFLOW_RETIREMENT.md`). `expose_webhook.py` still routes to
   `/api/webhooks/`, which has no route — `progress.md` lists it under dead
   artifacts. There is nothing to reuse.
2. **`<user_id>/<path>` is the wrong URL shape.** A user id is a small integer,
   so that URL is guessable, and a receiver that answers differently for a real
   user than an invented one is an enumeration oracle. The shipped receiver is
   `/api/orchestrator/hooks/<secret>/`: a generated 48-hex secret is the *only*
   credential, and every refusal — wrong secret, disabled trigger, agent not
   cleared for unattended runs — answers an identical 404.
3. **"A per-agent URL" is one too few.** `Trigger` is a row, so an agent may
   carry several hooks (staging and production, or one per calling system), and
   revoking one is deleting a row rather than breaking every integration.

## 2. What already existed

The backend shipped whole and is not the gap:

- `Trigger.mode='webhook'` with a unique generated `secret` (`agents/models.py`)
- `webhook_receive` (`agents/views/triggers.py`) — 202 and nothing else on
  success; the body is capped at 64 KB and appended as *context*, never as the
  goal; `_note_failure` counts refusals and disables the row at five
- `TriggerSerializer.webhook_url`, which surfaces the secret to the owner alone
- `agents/tests/test_triggers.py::WebhookTests` — nine tests, all about refusal

## 3. The actual gap

**Nothing in the frontend could create one.** `pages/Schedules.tsx` hardcoded
`mode: 'schedule'` in its one creation path, so the entire webhook feature was
reachable only from Django admin or curl. The card rendered a *Copy webhook URL*
button for a row no screen could produce — the same shape of failure as the todo
list that six green suites reported and no component drew.

## 4. What shipped

**Frontend — the creation path.** *New trigger* now asks schedule or webhook
first, and a webhook opens `WebhookEditor` rather than `ScheduleEditor`. The
card shows the full URL, a copyable `curl`, and — for webhooks — edit and rotate
instead of *Run now*.

**Backend — the secret is rotatable.** `POST /triggers/<id>/rotate/`. A URL that
is the only credential and can never be changed means a leak is permanent: the
sole remedy was delete-and-recreate, which discards `last_fired_at`, the failure
count, and the row every external system was pointed at. Rotation keeps the row
and changes the credential, which is what revocation of a leaked secret is.

**Backend — a webhook with nothing to ask is refused at save.** The receiver
already 404s when both the trigger's goal and the agent's prompt are blank, but
that 404 is indistinguishable from a wrong secret by design, so a misconfigured
hook was silently dead for ever. The serializer now refuses it at creation with
a sentence, and the receiver's own branch counts the failure so the row
surfaces it rather than only the server log.

## 5. Deliberate cuts

Each of these is a control the runtime would not read, and CLAUDE.md has four
worked examples of what shipping one costs:

- **`overlap`** — `webhook_receive` calls `start_agent_run` directly and never
  consults the policy. Offering skip/queue/cancel would be a switch that moves
  nothing.
- **`timezone`, `starts_at`/`ends_at`** — `window_state` is read by the sweep,
  and the sweep is not on the webhook path. A live window for webhooks is a real
  feature; it is a backend change first, not a form field.
- **Run now** — `trigger_run_now` refuses non-schedules on purpose: a test button
  that bypassed the unauthenticated path would prove the button works and
  nothing else. The editor shows the `curl` that exercises the real path.
- **Signature verification (HMAC over the body)** — the right long-term answer
  for a hook receiving from a system that supports it, and a larger change than
  a URL secret: it needs a per-sender shared key, a header name per provider,
  and a replay window. Named here so it is a decision rather than an omission.
