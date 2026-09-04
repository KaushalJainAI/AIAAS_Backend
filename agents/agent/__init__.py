"""
What an agent is at runtime.

`runtime` executes a run under its grants and autonomy level, `orchestrator`
runs the fan-out when it delegates, and `stream` carries it to the client. The
agent's HTTP surface is `orchestrator.views.agents` (CRUD) and
`orchestrator.views.runs` (execute, approve, reject, steer); its persistence is
a `SubAgent` row.
"""
