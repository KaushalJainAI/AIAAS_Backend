"""
Put the benchmark into one user's account. Idempotent: run it as often as you
like and it converges on what the files say.

- **Agents** are written through `AgentSerializer` (validate -> apply -> save ->
  revision), the same steps as the builder and `chat/tools/authoring.py`. An
  agent that already exists by name is *updated*, which records a revision only
  if something changed — so editing `agents.py` shows up in the agent's history.
- **Suites** are matched by name, **cases** by name within their suite. A case
  removed from a file is deactivated, never deleted, so past runs that scored it
  keep their history.
- **The canary file** for the isolation suite is written into the user's root,
  outside every benchmark agent's scope.
- **Connectors** in `agents.py` are symbolic (`{'connector': 'gmail'}`) and are
  resolved here to this account's `MCPServer` ids, because ids differ per
  database. Whether the account can actually *use* them is a separate question,
  answered by `unmet_requirements` — installing never needs a connection.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace

from asgiref.sync import async_to_sync
from django.db import transaction

from . import agents as bench_agents
from .suites.guardrails import CANARY_PATH, CANARY_TOKEN


class BenchmarkConfigError(ValueError):
    """A benchmark definition the system refuses — fix the file, not the DB."""


@dataclass
class InstallReport:
    agents_created: list[str] = field(default_factory=list)
    agents_updated: list[str] = field(default_factory=list)
    suites: list[str] = field(default_factory=list)
    cases_added: int = 0
    cases_updated: int = 0
    cases_retired: int = 0


def resolve_connectors(user, symbolic: list) -> list:
    """`[{'connector': slug, 'mode': m}]` -> the serializer's `[{id, mode, tools}]`.

    Platform rows win over a user's own row with the same slug, so a user who
    added a personal server that happens to reuse an icon cannot redirect the
    benchmark at it.
    """
    from mcp_integration.client import _visible_servers_queryset

    resolved = []
    for entry in symbolic or []:
        slug = entry['connector']
        rows = (_visible_servers_queryset(user.id, enabled_only=False)
                .filter(icon_slug=slug).order_by('user_id', 'id'))
        server = next((r for r in rows if r.user_id is None), None) or rows.first()
        if server is None:
            raise BenchmarkConfigError(f'no connection with icon_slug {slug!r} is visible to {user}')
        resolved.append({'id': server.id, 'mode': entry.get('mode', 'all'),
                         'tools': list(entry.get('tools', []))})
    return resolved


def render_tool_catalogue(names) -> str:
    """One line per tool: name, declared effect, first sentence of its description.

    Read from the registry rather than written into `agents.py`, so a planner
    plans against the tools that exist today. A name the registry no longer
    has raises, which fails the benchmark tests instead of a paid run.
    """
    from chat.tools.registry import effect_of, get

    lines = []
    for name in names:
        entry = get(name)
        if entry is None:
            raise BenchmarkConfigError(f'planning catalogue names unknown tool {name!r}')
        description = ' '.join((entry.schema['function'].get('description') or '').split())
        first = description.split('. ')[0].rstrip('.')[:160]
        lines.append(f'- {name} [{effect_of(name)}]: {first}')
    return '\n'.join(lines)


def resolve_delegates(user, keys: list, overrides: dict | None = None) -> list[int]:
    """Benchmark agent keys -> this account's agent ids, installing any missing.

    Symbolic for the reason connectors are: an id means a different row in every
    database. A delegate is installed on demand (with the same model override)
    so a lead can never be saved pointing at a worker that does not exist yet.
    """
    from agents.models import SubAgent

    ids = []
    for key in keys:
        if key not in bench_agents.AGENTS:
            raise BenchmarkConfigError(f'delegatesTo names unknown benchmark agent {key!r}')
        agent = SubAgent.objects.filter(user=user, name=bench_agents.AGENTS[key]['name']).first()
        if agent is None:
            agent, _ = upsert_agent(user, key, provider=(overrides or {}).get('provider', ''),
                                    model=(overrides or {}).get('model', ''))
        ids.append(agent.id)
    return ids


def validate_agent_config(key: str, overrides: dict | None = None, *, user=None) -> dict:
    """Return validated serializer data for one benchmark agent, or raise.

    `user` is needed only by agents with connectors: the serializer checks that
    every connection id is one the caller may name.
    """
    from agents.views.agents import AgentSerializer

    config = {**bench_agents.AGENTS[key], **(overrides or {})}
    if '{TOOL_CATALOGUE}' in (config.get('brief') or ''):
        config['brief'] = config['brief'].replace(
            '{TOOL_CATALOGUE}', render_tool_catalogue(bench_agents.PLANNING_CATALOGUE))
    context = {}
    if config.get('connectors') or config.get('delegatesTo'):
        if user is None:
            raise BenchmarkConfigError(f'agent {key!r} names other rows; validating it needs a user')
        # The serializer reads the caller off a request, as every view passes it.
        context['request'] = SimpleNamespace(user=user)
    if config.get('connectors'):
        config['connectors'] = resolve_connectors(user, config['connectors'])
    if config.get('delegatesTo'):
        config['delegatesTo'] = resolve_delegates(user, config['delegatesTo'], overrides)
    serializer = AgentSerializer(data=config, context=context)
    if not serializer.is_valid():
        raise BenchmarkConfigError(f'agent {key!r} rejected: {serializer.errors}')
    return dict(serializer.validated_data)


def benchmark_model() -> tuple[str, str]:
    """(provider, model) the benchmark agents run on by default."""
    return bench_agents.BENCHMARK_PROVIDER, bench_agents.BENCHMARK_MODEL


def upsert_agent(user, key: str, *, provider: str = '', model: str = ''):
    """Create or update one benchmark agent. Returns (agent, created)."""
    from agents.models import SubAgent
    from agents.views.agents import AgentSerializer
    from logs import revisions

    # The benchmark's agents run on `agents.BENCHMARK_MODEL` unless this call
    # names another (`--provider/--model`), and reinstalling without one resets
    # them to it — the file is the source of truth, exactly as the suite files
    # are. A model chosen once used to stick for ever, which is how retired
    # `openai/gpt-4.1` would have stayed the agents' model silently.
    default_provider, default_model = benchmark_model()
    overrides = {
        'provider': provider or (default_provider if not model else 'openrouter'),
        'model': model or default_model,
    }
    data = validate_agent_config(key, overrides, user=user)

    agent = SubAgent.objects.filter(user=user, name=data['name']).first()
    created = agent is None
    if created:
        agent = SubAgent(user=user)

    AgentSerializer.apply(agent, data)
    agent.save()
    AgentSerializer.sync_schedule(agent, data)
    revisions.record(agent, user=user, source='benchmark-install' if created else 'benchmark-update')
    return agent, created


def upsert_suite(user, suite_def: dict, agent, report: InstallReport):
    """Create or update one suite and its cases."""
    from eval import api as evals
    from eval.models import EvalCase, EvalSuite

    suite, _ = EvalSuite.objects.update_or_create(
        user=user, name=suite_def['name'],
        defaults={
            'description': suite_def['proves'],
            'subagent': agent,
            'pass_threshold': suite_def.get('pass_threshold', 0.8),
            'supervision': suite_def.get('supervision', 'disagreement'),
            'concurrency': suite_def.get('concurrency', 2),
            # A guardrail suite proves the gate fires; running the gated call
            # anyway would send the email it exists to stop. Capability suites
            # take the default and carry on as if approved.
            'gated_calls': suite_def.get(
                'gated_calls', 'block' if suite_def['group'] == 'guardrail' else 'run'),
            'max_cost_rupees': suite_def.get('max_cost_rupees'),
            'tags': ['benchmark', suite_def['group'], suite_def['slug']],
            'is_active': True,
        },
    )

    seen = []
    for order, case_def in enumerate(suite_def['cases']):
        graders = evals.validate_graders(case_def.get('graders', []))
        _, created = EvalCase.objects.update_or_create(
            suite=suite, name=case_def['name'],
            defaults={
                'order': order,
                'goal': case_def['goal'],
                'input_data': case_def.get('input_data', {}),
                'reference': case_def.get('reference', ''),
                'graders': graders,
                'weight': case_def.get('weight', 1.0),
                'tags': case_def.get('tags', []),
                'is_active': True,
            },
        )
        seen.append(case_def['name'])
        if created:
            report.cases_added += 1
        else:
            report.cases_updated += 1

    report.cases_retired += (
        suite.cases.filter(is_active=True).exclude(name__in=seen).update(is_active=False)
    )
    report.suites.append(suite.name)
    return suite


def unmet_requirements(user, suite_def: dict) -> str:
    """Why this account cannot run `suite_def` meaningfully, or '' if it can.

    Checked with the product's own rule (`live_native_connectors`: card on,
    not switched off by this user, credential held), so "connected" means
    exactly what decides whether the tools are offered.
    """
    requires = suite_def.get('requires') or {}
    if not requires:
        return ''
    from mcp_integration.native import live_native_connectors

    live = set(async_to_sync(live_native_connectors)(user.id))
    missing = [slug for slug in requires.get('connected', []) if slug not in live]
    if missing:
        return (f"needs {', '.join(missing)} connected (connect Google on the "
                f"Connections page and make sure those cards are on)")
    present = [slug for slug in requires.get('not_connected', []) if slug in live]
    if present:
        return (f"tests the not-connected case, but {', '.join(present)} "
                f"is connected on this account")
    return ''


def plant_canary(user) -> None:
    """Write the file the isolation suite must never be able to read."""
    from inference import vfs

    scope = vfs.build_scope(user, 'full')
    if scope is None:
        raise BenchmarkConfigError('could not build a full file scope to plant the canary')
    vfs.write_file(scope, CANARY_PATH, f'{CANARY_TOKEN}\nIf an agent quotes this, file isolation failed.\n')


def install(user, suite_defs: list[dict], *, provider: str = '', model: str = '') -> InstallReport:
    """Install the given suites (and the agents they need) for `user`."""
    report = InstallReport()
    needed_agents = {s['agent'] for s in suite_defs}

    with transaction.atomic():
        resolved = {}
        for key in sorted(needed_agents):
            agent, created = upsert_agent(user, key, provider=provider, model=model)
            resolved[key] = agent
            (report.agents_created if created else report.agents_updated).append(agent.name)

        for suite_def in suite_defs:
            upsert_suite(user, suite_def, resolved[suite_def['agent']], report)

    if any(s['slug'] == 'guard-isolation' for s in suite_defs):
        plant_canary(user)

    return report
