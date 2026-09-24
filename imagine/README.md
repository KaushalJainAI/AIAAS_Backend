# `imagine/`: image, video and audio generation

The **Studio** page. Two ways in:

- **Form** ("Advanced"): pick a model, write a prompt, press Generate.
- **Conversational agent**: describe what you want over the `ws/imagine-agent/`
  WebSocket. It works out the request, asks before spending, then generates.

Both use the same generation code. Finished media is saved into your file
tree. Generation goes through OpenRouter with the user's own key.

Design: [`docs/IMAGINE.md`](../docs/IMAGINE.md).

## Data (`models.py`)

| Model | What it is |
|---|---|
| `Generation` | One generation request and its result |
| `ImagineConversation`, `ImagineMessage` | The conversational agent's chats |

## Files

| File | What it does |
|---|---|
| `views.py`, `urls.py`, `serializers.py` | `/api/imagine/`: the form, history, model capabilities |
| `consumers.py` | The WebSocket for the conversational agent |
| `agent/graph.py` | The agent's steps: understand → ask approval → generate → reply |
| `agent/intent.py` | Turning a message into a structured generation request |
| `agent/hitl.py` | The approval gate |
| `services/dispatcher.py` | **The one generation path** both the form and the agent use |
| `services/openrouter.py` | The OpenRouter API client |
| `services/catalog.py`, `services/capabilities.py` | Which media models exist and what they accept |
| `services/documents.py` | Saving a result as a file in your tree |
| `services/events.py` | Pushing progress over the WebSocket |
| `validation.py` | What the chosen model will actually accept |
| `tasks.py` | Background jobs (video polling) |

The chat tool `generate_image` (`chat/tools/media.py`) is separate: that is the
AI making an image in the middle of a conversation.
