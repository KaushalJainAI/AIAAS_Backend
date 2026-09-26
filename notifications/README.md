# `notifications/`: the bell, reminders and the digest

- **Notifications**: rows in the bell menu, also pushed live over the
  `ws/hitl/` socket and, if enabled, as browser push notifications.
- **Approval reminders**: a run waiting for you is nudged at +0, +1 hour and
  +1 day, then left alone.
- **Daily digest**: one summary email a day of what is waiting on you.
- **Scheduled reminders**: reminders you (or the AI) set for a time.

Design: [`docs/NOTIFICATION_SERVICE.md`](../docs/NOTIFICATION_SERVICE.md).

## Data (`models.py`)

| Model | What it is |
|---|---|
| `Notification` | One row in the bell menu |
| `NotificationPreference` | Your settings: digest time, hourly nudge on/off |
| `HITLReminderSchedule` | The reminder timetable for one pending approval |
| `ScheduledNotification` | A reminder set for a time or a repeating interval |
| `PushSubscription` | A browser registered for push notifications |

## Files

| File | What it does |
|---|---|
| `utils.py` | `create_notification`: **the way to create a notification**. Saves it, pushes it live, may email it |
| `reminders.py` | The approval-reminder engine and the digest |
| `scheduled.py` | User-set reminders |
| `webpush.py` | Browser push (works with the tab closed) |
| `signals.py` | Keeps the reminder timetable in step with its approval request |
| `tasks.py` | Celery entry for the sweep |
| `views.py`, `urls.py`, `serializers.py` | `/api/notifications/...` |

## Email

Email belongs to the daily digest (at most one a day). `NOTIFICATIONS_EMAIL_ENABLED`
defaults to `False`. When you call `create_notification`, pass
`send_email=False` unless the notification really should be emailed; the
scheduled reminders a user asks for are the other exception.
`notifications/tests/test_email_policy.py` fails if a new caller can email.

## Management commands

The two sweeps run inside the server, on the scheduler loop
(`agents/scheduler.py`); no cron is needed. `send_hitl_reminders` and
`send_scheduled_notifications` run the same sweeps by hand.
`generate_vapid_keys` makes the keys for browser push.

The scheduled sweep **claims each firing before sending it**, so two sweeps
running at once (say the loop and a manual run) never send a reminder twice.
If sending fails, the claim is handed back and the next sweep retries.

## Tests

`notifications/tests/`: `test_reminders.py`, `test_agent_notifications.py`.
