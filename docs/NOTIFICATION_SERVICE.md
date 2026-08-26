# Notification Service & Real-time Updates

The AIAAS Notification Service provides a unified system for alerting users about system events, workflow status updates, and interactive "Human-In-The-Loop" (HITL) requests. It combines persistent database logging with real-time WebSocket broadcasting.

## 1. Architecture Overview

The system is divided into two primary layers:
1.  **Persistent Notifications (REST):** Asynchronous alerts stored in the database for historical reference (e.g., "Workflow Failed", "Image Ready").
2.  **Real-time Streams (WebSockets):** Low-latency updates for active execution monitoring and interactive decision-making.

---

## 2. Persistent Notifications

Handled by the `notifications` app, these are intended for events that the user needs to see even if they weren't online when the event occurred.

### Data Model (`Notification`)
- **Type:** Categorizes the alert (`workflow_failed`, `new_message`, `hitl_request`, `image_ready`, `system`).
- **Target:** Specific `User` via ForeignKey.
- **Payload:** Title, Message, and a `JSONField` for additional context (e.g., `execution_id`).
- **State:** `is_read` boolean for tracking user interaction.

### API Endpoints
- `GET /api/notifications/`: Retrieves the user's notification history (sorted by newest first).
- `POST /api/notifications/{id}/mark_read/`: Marks a specific notification as read.
- `POST /api/notifications/mark_all_read/`: Bulk update for all unread notifications.

### Backend Usage
To trigger a notification from any part of the backend:
```python
from notifications.utils import create_notification

create_notification(
    user=user_instance,
    type='workflow_failed',
    title='Production Workflow Failed',
    message='Workflow "Data Sync" failed at node "PostgreSQL"',
    data={'execution_id': '...'}
)
```

### Email Delivery
Persistent notifications can also be delivered by email through Django's email backend.

Environment settings:
- `NOTIFICATIONS_EMAIL_ENABLED=True` enables email fan-out.
- `NOTIFICATIONS_EMAIL_TYPES=` optionally limits email delivery to a comma-separated list of notification types, such as `workflow_failed,hitl_request,image_ready`.
- `NOTIFICATIONS_EMAIL_SUBJECT_PREFIX=[AIAAS]` prefixes outbound email subjects.
- `EMAIL_BACKEND`, `EMAIL_HOST`, `EMAIL_PORT`, `EMAIL_HOST_USER`, `EMAIL_HOST_PASSWORD`, `EMAIL_USE_TLS`, and `DEFAULT_FROM_EMAIL` configure SMTP delivery.

Per-notification overrides:
```python
create_notification(
    user=user_instance,
    type='system',
    title='Maintenance Notice',
    message='The system will restart tonight.',
    send_email=True,  # force email for this notification
)

create_notification(
    user=user_instance,
    type='new_message',
    title='New Message',
    message='You have a new chat response.',
    data={'send_email': False},  # keep this one in-app only
)
```

---

## 2b. HITL Reminders

A blocked run waits on a human. The reminder engine (`notifications/reminders.py`)
exists so that waiting is bounded, without turning one blocked agent into an
inbox full of mail.

### The three channels

Each is independent, and they deliberately do **not** share a transport.

| Channel | When | Transport | Off switch |
|---------|------|-----------|------------|
| **Escalation** | +0, +1h, +1d after an unanswered request, then stops | Device push + in-app | `hitl_escalation_enabled` |
| **Hourly** | Once an hour while *anything* is pending | Device push + in-app | `hourly_reminders_enabled` (**off by default**) |
| **Daily digest** | The user's chosen local time | **Email** + in-app + device push | `daily_digest_enabled` |

The escalation offsets are measured from the request's `created_at`, not from
the previous send, so a sweep that runs late cannot push the whole ladder
forward.

**Email is the digest's alone.** Escalation and hourly pass `send_email=False`
into `create_notification` explicitly, so flipping `NOTIFICATIONS_EMAIL_ENABLED`
on cannot start mailing every nudge. The digest is capped at one per calendar
day by `NotificationPreference.last_digest_sent_on`, a date in the user's own
timezone — the field is read-only over the API and in admin, because clearing
it would re-open the cap.

The digest claims its day even when nothing is pending. Otherwise a request
arriving at 22:00 would trigger a "daily" digest at 22:00.

### Data model

- **`NotificationPreference`** (one per user, created on first access) — the
  toggles above, `daily_digest_time`, `timezone` (blank falls back to
  `UserProfile.timezone`, then UTC), quiet hours, and the two bookkeeping
  fields that enforce the caps.
- **`HITLReminderSchedule`** (one per `HITLRequest`) — `stage`, `next_due_at`,
  `reminders_sent`. `next_due_at` is the only field the sweep queries, and is
  set to NULL once the ladder is exhausted or the request stops being pending,
  so finished requests drop straight out of the index.

`notifications/signals.py` hooks `post_save` on `HITLRequest` rather than the
call sites: requests are created from `agents/supervision/approval_gates.py`, the
agent runtime and the imagine agent, and a new site would otherwise silently
opt out of reminders. The stage-0 nudge fires from `transaction.on_commit`, so
it is immediate rather than waiting for the next sweep, and a request rolled
back by a failing execution never produces a notification.

### Running the sweep

The sweep is idempotent — each channel records what it sent, so running it more
often than scheduled is harmless.

```bash
# Production: one beat process (never more — beat must be a singleton)
celery -A workflow_backend beat -l info
docker compose --profile async up beat

# Anywhere, no broker required
python manage.py send_hitl_reminders
python manage.py send_hitl_reminders --dry-run
```

`HITL_REMINDER_SWEEP_SECONDS` (default 300) sets the beat interval. Local dev
runs with `RUN_WORKFLOWS_ASYNC=False` and no Redis, which is exactly why the
management command exists: a beat-only design would silently never fire there.

### Device notifications

`push_device_notification` sends over the per-user `hitl_{user_id}` channel
group as `hitl.reminder`; `HITLNotificationConsumer` relays it as a `reminder`
frame. Two clients listen on `ws/hitl/`:

- **Web frontend** — `hooks/useHITLReminders.ts`, mounted once in the
  authenticated `Layout`, raises a browser `Notification`.
- **BrowserOS** — `hooks/useHITLReminders.ts`, mounted in `DesktopPage` and
  always on while signed in,
  raises a desktop notification through `notify()`.

 **Scope**: the browser Notifications API only fires while a tab is open,
backgrounded or not. Delivery to a *fully closed* browser needs Web Push
(service worker + VAPID), which is not implemented — the daily email digest is
the closed-browser channel. Quiet hours suppress the OS ping only; the in-app
row is still written and the ladder still advances, so a request cannot get
stuck behind a permanently swallowed nudge.

### Settings

`NotificationsTab` → `ReminderPreferences` (`components/settings/`) exposes all
of it. The permission prompt is fired from the toggle's click handler, because
browsers ignore (Chrome) or reject (Safari) an ungated `requestPermission()`.

---

## 3. Real-time WebSocket Streams

Real-time updates are managed via **Django Channels** and are routed through `streaming/routing.py`.

### Execution Stream (`ws/execution/{execution_id}/`)
Provides granular updates for a specific workflow run.
- **execution.event:** Emitted when a node starts, finishes, or fails. Used to update the visual canvas in real-time.
- **execution.state_sync:** Sent immediately upon connection to sync the current status of all nodes.

### HITL Stream (`ws/hitl/`)
A dedicated, user-wide stream for **Human-In-The-Loop** requests.
- This allows a user to receive approval requests from *any* active workflow without being on that specific workflow's page.
- **Message Types:**
    - `new_request`: Triggered when an agent requires human intervention (approval, clarification, error recovery).
    - `response_ack`: Confirmation that the user's decision was received and processed.

---

## 4. Human-In-The-Loop (HITL) Flow

HITL is a critical feature that allows autonomous agents to "pause" and ask for permission or data.

1.  **Request Generation:** The Orchestrator or a specific Node detects a need for input and creates a `HITLRequest` record.
2.  **Notification:**
    - A `Notification` record is created (Persistent).
    - A WebSocket message is pushed to the `hitl_{user_id}` group (Real-time).
3.  **User Response:**
    - The user sees a popup or notification in the UI.
    - User sends a response via WebSocket (`type: 'hitl_response'`) or via the REST API.
4.  **Resumption:** The `ExecutionConsumer` updates the database, signals the background executor, and the workflow resumes from the paused state.

---

## 5. Frontend Integration

### Hooks
The frontend uses a custom hook `useHITLWebSocket` to maintain the user-wide notification connection.
- **Path:** `better-n8n-frontend/src/hooks/useWebSocket.ts`
- **Transport:** the hook is an adapter over `src/lib/websocket.ts`, which owns URL
  resolution (`VITE_WS_URL`), exponential reconnect backoff, and the remount race guard.
  Connection behaviour is changed there, not per hook.
- **Component:** `NotificationsTab.tsx` provides the UI for viewing and managing persistent notifications.

### Notification Types
| Type | Description | UI Action |
| :--- | :--- | :--- |
| `workflow_failed` | A workflow encountered a terminal error. | Link to Logs |
| `hitl_request` | Agent is waiting for your approval. | Open Modal / Decision UI |
| `image_ready` | DALL-E or Stable Diffusion task finished. | View Image |
| `system` | Maintenance or system-wide alerts. | Toast Notification |
