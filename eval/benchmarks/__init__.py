"""
The benchmark: practical tasks that say whether this system actually works.

`eval/` is the engine (suites, graders, sweeps, human review). This package is
the *content* — the questions worth asking it — and lives in code, not rows, for
the reason `agents/gallery.py` gives about templates: a row seeded by a
migration drifts from the validator that checks it, while a dict in a file is
checked by a test on every run.

    eval/benchmarks/
      README.md          <- start here: what each suite proves, how to run it
      agents.py          <- the agents under test (flat AgentConfig dicts)
      suites/            <- one file per capability, each a SUITE dict
      install.py         <- writes agents + suites into one user's account
      report.py          <- turns finished sweeps into a markdown scorecard
      reports/           <- scorecards land here, one file per run

The command is `python manage.py benchmark` (list | install | run | report).

A suite and its agent are paired by name (`SUITE['agent']` is a key of
`agents.AGENTS`) rather than by id, so the same benchmark installs identically
into any account.
"""
from __future__ import annotations

from typing import Any

from .suites import ALL_SUITES


def suites() -> dict[str, dict[str, Any]]:
    """slug -> suite definition, in the order the README presents them."""
    return {suite['slug']: suite for suite in ALL_SUITES}


def get(slug: str) -> dict[str, Any] | None:
    return suites().get(slug)
