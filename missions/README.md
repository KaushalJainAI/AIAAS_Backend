# `missions/`: goals that span many runs

One agent run lasts at most about 2 hours. A **mission** is a bigger goal
carried out by a chain of normal runs. Each run starts through the usual door
(`start_agent_run`), so all the usual limits apply. Between runs the mission
keeps a plan and a notebook file (`/Agents/<name>/missions/<id>/NOTES.md`).

## Files

| File | What it does |
|---|---|
| `models.py` | `Mission`: goal, status, plan, budget, deadline, when to wake next |
| `service.py` | After a run ends: done, waiting, next run, or paused? |
| `sweep.py` | Starts runs for missions that are due |
| `tasks.py` | Celery entry |
| `urls.py` | `/api/missions/`: create, list, pause, resume, cancel |

Start one from chat with `/goal`, or from the Missions section at the top of
the Activity page in the web app (`src/pages/Runs.tsx`, through
`src/api/missions.ts`). `/missions` redirects to `/runs`.

Management command: `run_missions`.

> **Known gap (2026-09-26):** nothing runs the mission sweep in production.
> The in-process scheduler runs every other periodic job, but this one waits
> for each run to finish (`start_agent_run_and_wait`), which would hold a
> web-server thread for up to two hours. Until `sweep.py` starts runs detached
> (like `agents/scheduler.py::launch`), a mission starts its first run and does
> not advance. See `NOT_IN_PROCESS` in `agents/scheduler.py`.
