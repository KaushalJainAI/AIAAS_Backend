"""
The suites, one file per capability.

Every suite is a dict of this shape (checked by `eval/tests/test_benchmarks.py`):

    {
        'slug':        'data-analysis',          # CLI name, unique
        'group':       'capability' | 'guardrail',
        'name':        'Data analysis',          # EvalSuite.name once installed
        'agent':       'analyst',                # key in benchmarks.agents.AGENTS
        'proves':      'One sentence: what a pass means for the product.',
        'pass_threshold': 0.8,
        'requires': {'connected': ['gmail']},   # optional; see suites/connectors.py
        'cases': [
            {
                'name':   'Short, unique within the suite',
                'goal':   'The prompt the agent receives',
                'input_data': {...},   # optional; appended as labelled JSON
                'reference':  '...',   # optional; what good looks like (for llm_judge + reviewers)
                'graders': [{'type': 'contains', 'value': '...'}],
                'tags':   ['...'],
            },
        ],
    }

Prefer deterministic graders (`contains`, `regex`, `tool_used`,
`paused_for_approval`). Reach for `llm_judge` only for what no string match can
decide, and pair it with at least one deterministic check so a malfunctioning
judge cannot pass a case on its own.
"""
from .connectors import SUITES as CONNECTORS
from .data_analysis import SUITE as DATA_ANALYSIS
from .files import SUITE as FILES
from .guard_work import SUITE as GUARD_WORK
from .guardrails import SUITES as GUARDRAILS
from .instructions import SUITE as INSTRUCTIONS
from .planning import SUITES as PLANNING
from .research import SUITE as RESEARCH
from .work_analyst import SUITE as WORK_ANALYST
from .work_docs import SUITE as WORK_DOCS
from .work_long import SUITE as WORK_LONG
from .work_office import SUITE as WORK_OFFICE
from .work_ops import SUITE as WORK_OPS
from .work_research import SUITE as WORK_RESEARCH

#: Capabilities first (does it do the job?), then guardrails (does it stop
#: where it should?). The README lists them in this order.
ALL_SUITES = [INSTRUCTIONS, RESEARCH, DATA_ANALYSIS, FILES, *PLANNING, *GUARDRAILS, *CONNECTORS]

#: The harder, realistic tier (2026-09-17): multi-file workspaces graded on the
#: files produced, each case repeated to measure reliability. Kept as its own
#: group so `--group work` runs exactly these; guard-work is still a guardrail.
WORK_SUITES = [WORK_ANALYST, WORK_OPS, WORK_DOCS, WORK_RESEARCH, WORK_LONG, WORK_OFFICE,
               GUARD_WORK]
ALL_SUITES += WORK_SUITES
