"""
Stock agent configurations — the ones that prove configuration can replace code.

`RESEARCH` is the test of the whole architecture. `chat/tools/web.py`'s
`deep_research` is a hardcoded pipeline: fan out across queries, scrape the
pages, return `{type, queries, sources, text}`. Everything it does is now
expressible as configuration — the prompt says how to work, `fanout` says how
wide, `output_schema` says what shape to come back in, and `tool_grants` says
what it may touch.

The hardcoded tool is deliberately **not** deleted. It makes zero model calls
and this makes at least one, so it is slower and dearer per invocation; that is
the real price of the generalisation and it should be paid knowingly. Keeping
both lets the two be compared on the same question before anything is removed.
`agents/tests/test_contracts.py::SupersessionTests` is where that comparison
lives.
"""
from __future__ import annotations

from typing import Any

RESEARCH_PROMPT = """\
You research a topic in depth and report what you actually found.

How to work:
- Break the topic into 2-4 distinct angles and search each one. Different
  angles, not rephrasings of the same query.
- Read the pages you find. A search snippet is not a source; open it.
- Corroborate anything load-bearing across at least two independent pages.
- When sources disagree, say so and say which you find more credible and why.
  Do not average them into a claim neither one makes.
- Never state a fact you did not read. If you could not find something, say
  that you could not find it.
"""

#: name -> the `SubAgent` field values that configure it.
STOCK: dict[str, dict[str, Any]] = {
    'Deep Research': {
        'description': (
            'Researches a topic across several angles, reads the pages, and '
            'reports findings with sources.'
        ),
        'prompt': RESEARCH_PROMPT,
        'tool_grants': {
            'webSearch': True, 'scrape': True,
            'codeExecution': False, 'shell': False, 'fileOps': False,
            'rag': False, 'mcp': False, 'subAgents': False,
        },
        'guardrails': {'autonomy': 'full', 'spendCapRupees': 500},
        'output_schema': {'contract': 'research'},
        # `parallel` only: `mode` was stored here and read by nothing —
        # `run_fanout` takes a width and returns results in task order, which is
        # the whole of what "collect" was describing.
        'fanout': {'parallel': 4},
        'agent_context': {},
        'tags': ['research', 'stock'],
        'icon': 'search',
        # On, because this is the configuration meant to be delegated to — it
        # is the worker in `invoke_subagent(agent_id=...)`, and delegation runs
        # as `caller='orchestrator'`, which the unattended gate covers.
        'allow_unattended': True,
        'status': 'active',
    },
}


def build(user, name: str = 'Deep Research'):
    """Return an unsaved `SubAgent` configured from the stock definition."""
    from agents.models import SubAgent

    config = STOCK.get(name)
    if config is None:
        raise KeyError(f'No stock agent named {name!r}.')

    return SubAgent(
        user=user,
        name=name,
        llm_provider='openrouter',
        llm_model='',
        **config,
    )
