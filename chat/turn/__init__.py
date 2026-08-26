"""
Running one conversational turn.

The whole path from a user message to a persisted answer: `pipeline` drives it,
`agent` is the tool loop, `llm.access` talks to the provider (see the `llm`
app), `history` decides what
the model is allowed to remember, `prompts` writes the system message,
`extraction` pulls tool calls back out of prose, `events` is the vocabulary the
turn emits, and `runs` owns a turn that outlives its HTTP request.

Nothing here knows about HTTP — see `chat.transport`.
"""
