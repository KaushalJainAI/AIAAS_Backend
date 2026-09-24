# `logs/`: the record of what agents did

When an agent runs, this app stores what happened, at three levels:

```
ExecutionLog   one run          (status, goal, answer, cost, who started it)
 └─ AgentTurn   one model call   (the model's full reasoning for that turn)
     └─ AgentStep  one tool call  (arguments, result, approval decision)
```

It also stores `SubAgentRevision`: a snapshot of an agent's settings, so every
run points at the exact config it ran with.

Full design: [`docs/AGENT_OBSERVABILITY.md`](../docs/AGENT_OBSERVABILITY.md).

## Data (`models.py`)

| Model | What it is |
|---|---|
| `ExecutionLog` | One run. `caller` says what started it (`chat`, `api`, `trigger`, `orchestrator`, `eval`). `parent_step` points at the tool call that delegated it, if any |
| `AgentTurn` | One model call inside a run, with its reasoning |
| `AgentStep` | One tool call inside a turn. Created *before* the tool runs |
| `SubAgentRevision` | An agent's settings at one point in time |
| `CostEntry` | Spending that isn't model tokens (images, SMS...) |
| `Feedback`, `RunSignal` | Thumbs up/down, and automatic quality signals |

## Files

| File | What it does |
|---|---|
| `models.py` | The tables above |
| `queries.py` | **Every** database read behind `/api/logs/`. Views stay thin |
| `views.py`, `urls.py` | `/api/logs/`: insights, run history, revisions |
| `revisions.py` | Saving a new revision only when settings really changed |
| `costs.py` | The only writer of `CostEntry` |
| `failures.py` | One-word reasons a run failed |
| `signals_api.py` | Recording implicit quality signals |

**Who writes the run records?** Not this app. `agents/agent/runtime.py`
creates the `ExecutionLog` when a run opens, and `agents/agent/stream.py`
writes each `AgentTurn` and `AgentStep` while the run happens.

## Tests

`logs/tests/`: `test_turns.py`, `test_delegation.py`, `test_revisions.py`,
`test_ledger.py`, `test_checkpoints.py`.
