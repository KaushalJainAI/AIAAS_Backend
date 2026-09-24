"""
Simulated services for eval worlds (`docs/EVAL_ENVIRONMENTS_PLAN.md`).

Each simulator answers one surface's tools from fixture JSON, deterministically:
same world, same calls, same answers. Pure Python — no Django, no network, no
clock — so a world behaves identically on every attempt and every machine.

The contract every simulator keeps:

* `TOOLS` — the real tool names it answers.
* `handles(name)` / `run(name, args) -> str` — `run` never raises; a bad call
  is an `"Error: …"` string or an `{"error": …}` object, the way the real
  tools answer one.
* `snapshot()` — the state graders read (`GradeContext.env`).
* `changes()` — what this attempt mutated, for the what-changed panel.
* `apply_expected(expect)` — perform the case's expected state, for the
  generation-time proof (`prove_case`).

Result shapes mirror the real tools (`chat/tools/google/`) field for field,
pinned by `test_sim_coverage.py::SimShapeTests`, so the agent cannot tell a
fixture from a service. Fixture schemas live in each module's docstring and
are validated by `eval/environment.py::validate_world`.
"""
