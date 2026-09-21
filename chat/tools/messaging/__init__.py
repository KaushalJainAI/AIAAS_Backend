"""Provider-neutral messaging tools, one set for four channels.

Five tools, not twenty: `message_channels`, `message_search`, `message_read`,
`message_draft`, `message_send`. Each channel is an adapter
(`slack.py`, `whatsapp.py`, `teams.py`, `sms.py`) implementing
`search / read / send / list_targets` and raising `Unsupported` for verbs the
platform cannot do — SMS has no search, WhatsApp search covers only messages
received through our webhook, Teams waits on admin consent. The tool returns
that reason as text, so the model plans around it instead of failing it.
"""
