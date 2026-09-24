# `sandbox/`: running the AI's Python code safely

Behind the `execute_python` and `run_python_on_files` tools. Not a Django app,
just a Python package.

**One entry point:** `engine.py::arun_code`. It picks an engine from the
`SANDBOX_ENGINE` setting:

| Engine | When | How safe |
|---|---|---|
| `service` | Production | Sends the code to a separate locked-down container (`../sandbox_service/`): no network, no secrets, memory and process limits, killed on timeout |
| `inprocess` | Local development only | `safe_execution.py`: blocks dangerous code by inspecting it, then runs it in a thread. **Much weaker** |

There is no automatic fallback from `service` to `inprocess`.

## Files

| File | What it does |
|---|---|
| `engine.py` | Picks the engine and runs the code |
| `service_client.py` | Talks to the sidecar container |
| `safe_execution.py` | The dev-only in-process engine |

Design: [`docs/SANDBOX_EXECUTION.md`](../docs/SANDBOX_EXECUTION.md).
Tests: `sandbox/tests/`, `sandbox_service/tests/`.
