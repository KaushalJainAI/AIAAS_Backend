# `messaging/`: Slack, WhatsApp, Teams, SMS, Telegram

Lets the AI send and read messages on chat platforms through one set of tools
(`chat/tools/talk.py`), whatever the channel.

## Data (`models.py`)

| Model | What it is |
|---|---|
| `MessagingAccount` | A connected channel account |
| `OutboundMessage` | A message we sent |
| `InboundMessage` | A message we received. Kept for a limited time (`retention.py`) |

## Files

| File | What it does |
|---|---|
| `channels.py` | Which channels exist, for the Connections page |
| `views.py`, `urls.py` | Webhooks the platforms call when a message arrives, plus account registration |
| `retention.py` | Deleting old inbound messages (`MESSAGING_RETENTION_DAYS`) |
| `tasks.py` | Background jobs |

The per-platform code lives in `chat/tools/messaging/`, one file per platform.

Management commands: `purge_inbound`, `register_telegram_webhook`.
