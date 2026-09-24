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

Start one from chat with `/goal`, or from the `/missions` page in the web app
(`src/pages/Missions.tsx`, through `src/api/missions.ts`).

Management command: `run_missions`.
