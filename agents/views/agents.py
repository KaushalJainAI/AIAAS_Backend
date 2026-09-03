"""
Agents API.

An agent is a `SubAgent` row.

This module owns the translation between the frontend's `AgentConfig` (camelCase,
flat) and the model's columns (snake_case, grouped by who reads them). That
translation lives in exactly one place on purpose: the permissions screen an
installer sees is rendered from `tool_grants` / `guardrails` / `requirements`,
and the runtime enforces from those same columns. If a second mapping existed
the screen could promise something the runtime never checks.

Everything here is scoped to `request.user`. Ids that point at other rows
(knowledge bases, skills) are re-checked against the caller — an agent that
could attach someone else's knowledge base would be a cross-tenant read.

What is *not* here: starting or intervening in a run (`views/runs.py`) and the
read-only canvas projections (`views/canvas.py`). Those are a run's lifecycle;
this is an agent's configuration, and the two change for different reasons.
"""
import logging

from django.db.models import Count, Q, Sum
from django.shortcuts import get_object_or_404
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from inference.models import KnowledgeBase
from llm.effort import LADDER as EFFORT_LADDER
from logs import revisions
from logs.models import ExecutionLog
from skills.models import Skill

from django.utils import timezone

from agents import budget, contracts
# Only the constant, and `orchestrator` imports nothing but the standard
# library — so this does not drag the agent runtime into the URLconf the
# way `views/runs.py` documents avoiding.
from agents.agent.orchestrator import MAX_PARALLEL_WORKERS
from agents.models import SubAgent
from agents.serializers import SUSPICIOUS_WORKFLOW_NAME_RE
from agents.spend import PRICED_SOURCES, rupees_for, rupees_for_usd
from workflow_backend.thresholds import (
    DEFAULT_RUN_SECONDS,
    MAX_RUN_SECONDS,
    MIN_RUN_SECONDS,
)
from agents.triggers import (
    is_valid as cron_is_valid,
    next_run_after,
    zone_is_valid,
)

logger = logging.getLogger(__name__)


# The closed sets. Anything outside them is rejected rather than stored, because
# an unknown tool key would sail through the permissions screen (which renders
# what it knows) and land in the runtime unrecognised.
# 'mcp' unlocks the user's own configured MCP servers. It is a grant rather
# than an always-on capability because those tools reach real systems under
# the user's credentials — see mcp_integration/credential_injector.py.
TOOL_KEYS = {'codeExecution', 'shell', 'webSearch', 'scrape', 'fileOps', 'rag', 'mcp',
             'subAgents'}
# `connectors` holds `MCPServer` ids, validated against what the user can
# actually see. It used to be a hardcoded set of six presentation slugs
# ({'gdrive', 'gmail', 'sheets', 'photos', 'calendar', 'slack'}) that nothing
# read: it had drifted from the connector catalogue in both directions —
# `photos` names no server that has ever existed, while Notion and Fetch could
# not be named at all — and the runtime resolved the whole of `mcp` regardless.
# A closed list in code cannot work here for the same reason MCP tool names
# cannot be allow-listed by name: a user's own server is a row, minted after
# this file was written.
# Two axes — what may be read, what may be written — collapsed into one field.
# `read_all_write_own` is the combination the other four cannot express: read
# the user's whole tree, write only the agent's own home. See inference/vfs.py.
FILE_ACCESS = {'none', 'readonly', 'scoped', 'read_all_write_own', 'full'}

# `trigger` (stored as `runtime_settings['invocationMode']`) is gone from the
# wire too. Nothing in the runtime ever branched on it, and the serializer
# already required a schedule for `maintenance` — so the schedule *was* the
# answer and this was a second place to contradict it. `to_config` still emits
# it, derived, because the builder shows how an agent is invoked; it is no
# longer settable, so the two cannot disagree.

# The autonomy ladder, loosest last. `plan` withholds every tool that could
# change anything, so a run can only look and report; `auto` stops asking about
# effects the user can undo (a file write lands in their recycle bin) while
# still stopping on the ones they cannot. The runtime resolves each level into
# a gate set and a policy — see `agents/agent/runtime.py::AUTONOMY_LADDER`,
# which is the authority on what each one means.
AUTONOMY = {'plan', 'review', 'ask', 'auto', 'full'}

# `egress` is gone from the wire (2026-09-03). It was read in exactly one place
# — to add "your sandbox has no network access" to the system prompt — and the
# other two values could never have been honoured: the production sandbox is a
# sidecar container on an internal-only Docker network, so `full` was not
# unenforced, it was unimplementable as built. The prompt line is now
# unconditional, which is the true statement. Existing rows keep the key; it is
# simply no longer read, offered or written.

# Which agent lifecycle states a user may set. `archived` is deliberately
# absent: `search_agents` already excludes it and there is no un-archive path,
# so offering it as a save would be a one-way door disguised as a dropdown.
SETTABLE_STATUS = {'draft', 'active', 'paused'}

# The result shapes an agent may be asked for, from the closed registry in
# `agents/contracts.py`. Blank means prose, which is the default and always
# valid. Closed rather than free-form JSON Schema for the reason that module
# states: the set of shapes the UI can render is closed by construction, so a
# schema language would let an agent declare one nothing can display.
OUTPUT_CONTRACTS = set(contracts.CONTRACTS)

#: How many tools one connection's `selected` mode may name. A ceiling rather
#: than a judgement: a server with more tools than this is one where picking
#: individually has stopped being the right control, and `read` is.
MAX_CONNECTOR_TOOLS = 60

#: Tag shape. Bounded because tags are matched by `search_agents` against every
#: agent a user owns, and an unbounded list is an unbounded scan.
MAX_TAGS = 12
MAX_TAG_CHARS = 30

# What a single run may reserve. `cpu` and `memoryMb` used to live here and are
# gone (2026-08-29): both were stored, validated, round-tripped to the builder
# and read by nothing, and the panel said so in an orange "COMING SOON" badge.
# Neither could have been honoured — `sandbox/safe_execution.py` runs user code
# on a worker thread inside this process, where there is no cgroup to hang a CPU
# quota off and no way to cap one thread's RSS — so they were a promise the
# architecture could not keep. What a run actually contends for is wall-clock
# time, and `maxRunSeconds` is that, enforced in `agents/budget.py`.

# Autonomy is the agent-level word for the supervision vocabulary the HITL
# path already reads. Map rather than duplicate, so that path keeps working
# for agents unchanged.
AUTONOMY_TO_SUPERVISION = {
    'plan': 'full',
    'review': 'full',
    'ask': 'error_only',
    'auto': 'error_only',
    'full': 'none',
}
# Written out rather than derived by inverting the map above. Five autonomy
# levels do not fit into three supervision values, so an inversion would pick
# whichever key happened to be inserted last and call it the answer. These are
# the deliberate representatives: the loosest level that still means each
# supervision setting.
SUPERVISION_TO_AUTONOMY = {'none': 'full', 'error_only': 'ask', 'full': 'review'}


class AgentSerializer(serializers.Serializer):
    """The knob board, as a wire contract.

    Not a ModelSerializer: the frontend shape is flat and camelCase while the
    model groups fields by which subsystem reads them, so every field needs
    mapping anyway and a ModelSerializer would only obscure that.
    """

    id = serializers.IntegerField(read_only=True)

    # Identity
    name = serializers.CharField(max_length=200)
    brief = serializers.CharField(required=False, allow_blank=True, default='')
    #: One line saying what this agent is *for*. Not decoration: `search_agents`
    #: reads it, and it is what a delegating agent chooses between. Until it
    #: reached the wire (2026-09-03) every agent a user built was blank to the
    #: parent trying to pick one.
    description = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=2000,
    )
    #: Matched by `_agent_haystack` in the same search, and the natural grouping
    #: for a user with thirty agents.
    tags = serializers.ListField(
        child=serializers.CharField(max_length=MAX_TAG_CHARS),
        required=False, default=list, max_length=MAX_TAGS,
    )
    #: draft | active | paused. Writable as of 2026-09-03: it was read-only, so
    #: the only way to stop a scheduled agent was to delete its schedule or the
    #: agent itself.
    status = serializers.ChoiceField(
        choices=sorted(SETTABLE_STATUS), required=False,
    )

    # Model
    provider = serializers.CharField(max_length=30, required=False, default='openrouter')
    model = serializers.CharField(max_length=100, required=False, allow_blank=True, default='')
    temperature = serializers.FloatField(required=False, default=0.2, min_value=0, max_value=2)
    # How hard the model is asked to think. Blank is the model's own default,
    # which is what every agent saved before this existed keeps — the same rule
    # as `connectors`: a field the builder gains must not silently change how
    # already-configured agents run. Validated against the ladder rather than
    # against a model's own rungs, because the model can be changed in the same
    # PATCH and `llm.access` snaps the level at call time anyway.
    effort = serializers.ChoiceField(
        choices=[('', 'Model default')] + [(level, level) for level in EFFORT_LADDER],
        required=False, allow_blank=True, default='',
    )

    # Sandbox
    #: `workdir` and `venv` were removed from the wire (2026-09-03) alongside
    #: `cpu`/`memoryMb` before them, for the same reason: stored, validated,
    #: round-tripped to the builder and read by nothing. The sandbox is a fixed
    #: sidecar image, and where an agent's files live is `fileAccess` plus the
    #: virtual filesystem — there is no directory for a user to choose.
    fileAccess = serializers.ChoiceField(choices=sorted(FILE_ACCESS), required=False, default='scoped')

    # Tools
    tools = serializers.DictField(child=serializers.BooleanField(), required=False)

    # Context it is given
    # Which connections this agent may reach — `MCPServer` ids, the second axis
    # to the `mcp` grant just as `fileAccess` is to `fileOps`. Empty means "any
    # the user has", which is what every agent saved before this was enforced
    # has; see `agents.agent.runtime.mcp_scope_for`.
    #: Either a bare `MCPServer` id (the pre-2026-09-03 shape, meaning every
    #: tool that connection offers) or `{id, mode, tools}`. Both are accepted
    #: for ever: a bare id is what every existing agent stores, and rewriting
    #: them would be a migration that narrows nobody but could widen someone.
    connectors = serializers.ListField(required=False, default=list)
    knowledgeBases = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    #: Which of the caller's agents this one may delegate to. Empty is any of
    #: them, which is what every agent saved before this existed means.
    delegatesTo = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list,
    )
    skills = serializers.ListField(child=serializers.IntegerField(), required=False, default=list)
    #: `useOrgContext` was removed from the wire (2026-09-03): it defaulted on,
    #: was stored on every agent, and no prompt builder ever read it. There is
    #: no workspace-level context to include or omit yet; it comes back with the
    #: feature, not before it.
    useEnvironment = serializers.BooleanField(required=False, default=False)

    # Result shape and shape of the work. Both columns have been read by the
    # runtime since the agent model landed — `contracts.resolve` at the top and
    # tail of every run, `run_fanout(parallel=…)` when a parent delegates a list
    # — and until now only `agents/stock.py` could set either. They are the two
    # fields the design leans on for "an agent that can only return prose cannot
    # stand in for a coded tool", so leaving them unreachable made that argument
    # untestable by anyone but us.
    outputContract = serializers.ChoiceField(
        choices=sorted(OUTPUT_CONTRACTS), required=False,
        allow_blank=True, default='',
    )
    #: How many workers one delegated fan-out runs at a time. `None`/absent is a
    #: single sequential turn; the runtime clamps to `MAX_PARALLEL_WORKERS`
    #: regardless, so this narrows and never widens.
    fanoutParallel = serializers.IntegerField(
        required=False, allow_null=True, default=None,
        min_value=1, max_value=MAX_PARALLEL_WORKERS,
    )

    # Invocation
    schedule = serializers.CharField(required=False, allow_blank=True, default='')
    # The zone the cron fields are read in. Defaults to UTC so an agent saved
    # by an older client keeps firing at exactly the instant it always did —
    # this is a widening, and a widening that moves existing schedules by five
    # and a half hours is not one.
    scheduleTimezone = serializers.CharField(
        required=False, allow_blank=True, default='UTC',
    )
    # The gate every unattended caller is checked against. Declared here because
    # `apply` reads `validated_data`, so an undeclared field is silently dropped:
    # `to_config` emitted `allowUnattended`, nothing could ever set it, and a
    # scheduled agent therefore armed a Trigger that the runtime then refused
    # five times before the sweep disabled it. Defaults False on purpose — a
    # request that does not mention it is not a request to widen the agent.
    allowUnattended = serializers.BooleanField(required=False, default=False)

    # Guardrails
    autonomy = serializers.ChoiceField(choices=sorted(AUTONOMY), required=False, default='ask')
    notifyOnHitl = serializers.BooleanField(required=False, default=True)
    reviewAgent = serializers.BooleanField(required=False, default=False)
    spendCapRupees = serializers.IntegerField(required=False, default=500, min_value=0)
    # The other half of the blast radius, and the one that was missing. The
    # spend cap is monthly and per agent: it says nothing about how long a
    # single run may hold an event-loop slot, a checkpointer and a DB
    # connection, which is what actually decides whether everyone else's runs
    # get served. Seconds because that is the unit the runtime compares
    # against; the builder shows minutes.
    maxRunSeconds = serializers.IntegerField(
        required=False, default=DEFAULT_RUN_SECONDS,
        min_value=MIN_RUN_SECONDS, max_value=MAX_RUN_SECONDS,
    )


    # Context lifecycle
    #: Which model folds the run's earlier steps when the window fills. Empty
    #: means the platform default (`CONTEXT_SUMMARY_MODEL`), which is a small
    #: NVIDIA model the platform holds a key for — so the fold works for a user
    #: who has connected nothing. Free-form rather than a choice field for the
    #: same reason `model` is: the catalogue lives in `AIModel` and a hardcoded
    #: enum here would go stale the first time a provider retires a name.
    summaryModel = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=200
    )
    summaryProvider = serializers.CharField(
        required=False, allow_blank=True, default='', max_length=30
    )
    recursiveContext = serializers.BooleanField(required=False, default=True)
    compaction = serializers.BooleanField(required=False, default=True)
    indexing = serializers.BooleanField(required=False, default=True)

    # Read-only: observed behaviour, computed from the ledger of runs.
    runs = serializers.IntegerField(read_only=True)
    unattended = serializers.IntegerField(read_only=True)
    spend = serializers.IntegerField(read_only=True)
    created_at = serializers.DateTimeField(read_only=True)
    updated_at = serializers.DateTimeField(read_only=True)

    # ---------------- validation ----------------

    def validate_name(self, value):
        name = value.strip()
        if not name:
            raise serializers.ValidationError('Agent name cannot be blank.')
        if SUSPICIOUS_WORKFLOW_NAME_RE.search(name):
            raise serializers.ValidationError('Agent name contains unsupported characters or terms.')
        return name

    def validate_tools(self, value):
        unknown = set(value) - TOOL_KEYS
        if unknown:
            raise serializers.ValidationError(
                f"Unknown tools: {', '.join(sorted(unknown))}. "
                f"Allowed: {', '.join(sorted(TOOL_KEYS))}."
            )
        return value

    def validate_connectors(self, value):
        """Reject any connection the caller cannot see, and normalise the shape.

        Ownership is the mirror of `_owned_ids`, but it cannot use it: a curated
        connector is a row with `user IS NULL` that everyone can see, so
        filtering on `user=request.user` would reject every connection the
        platform ships and accept only the user's own. `visible_server_ids_sync`
        is the same predicate the runtime resolves through, which is the point —
        a selection the builder accepts has to be one the toolbox will honour.

        Two shapes go in and one comes out. A bare id keeps working for ever
        (it is what every agent saved before 2026-09-03 holds) and is stored as
        given; an object carries the per-connection scope. What is *not* done
        here is validating tool names against the server's live catalogue: they
        are minted by a third party, a listing costs a subprocess and a timeout,
        and a name that has since disappeared has to be storable anyway or a
        save would start failing because someone else shipped a release. The
        toolbox intersects with the live catalogue at build time instead, which
        is the only place the answer is current.
        """
        from mcp_integration.client import visible_server_ids_sync
        from agents.connector_scope import MODES

        if not value:
            return []

        entries: dict[int, dict] = {}
        for raw in value:
            if isinstance(raw, bool):
                raise serializers.ValidationError('A connection is an id, not a boolean.')
            if isinstance(raw, int):
                entries.setdefault(raw, raw)
                continue
            if not isinstance(raw, dict):
                raise serializers.ValidationError(
                    'Each connection is an id, or an object with id, mode and tools.'
                )
            server_id = raw.get('id')
            if not isinstance(server_id, int) or isinstance(server_id, bool):
                raise serializers.ValidationError('Each connection needs an integer id.')
            mode = raw.get('mode', 'all')
            if mode not in MODES:
                raise serializers.ValidationError(
                    f"Unknown connection mode '{mode}'. Allowed: {', '.join(MODES)}."
                )
            tools = raw.get('tools') or []
            if not isinstance(tools, list) or any(not isinstance(t, str) for t in tools):
                raise serializers.ValidationError('`tools` is a list of tool names.')
            if len(tools) > MAX_CONNECTOR_TOOLS:
                raise serializers.ValidationError(
                    f'At most {MAX_CONNECTOR_TOOLS} tools may be named per connection.'
                )
            names = [t.strip()[:128] for t in tools if t.strip()]
            # `selected` naming nothing is a connection with no usable tools,
            # which nobody can have meant: it reads as the picking not being
            # finished, so it stays open until names arrive. The runtime reads
            # it the same way — one rule, stated in `connector_scope.parse`.
            if mode == 'selected' and not names:
                mode = 'all'
            # An object always wins over a bare id for the same connection: the
            # narrower statement is the deliberate one.
            entries[server_id] = (
                server_id if mode == 'all' and not names
                else {'id': server_id, 'mode': mode, 'tools': names}
            )

        unknown = set(entries) - visible_server_ids_sync(self.context['request'].user.id)
        if unknown:
            raise serializers.ValidationError(
                f"Unknown connections: {', '.join(str(i) for i in sorted(unknown))}."
            )
        return [entries[key] for key in sorted(entries)]

    def validate_tags(self, value):
        """Trimmed, de-duplicated, order kept.

        Order is kept rather than sorted because a user's first tag is the one
        they think of the agent as, and `search_agents` shows them in stored
        order.
        """
        seen: set[str] = set()
        tags: list[str] = []
        for raw in value:
            tag = ' '.join(str(raw).split())
            key = tag.lower()
            if tag and key not in seen:
                seen.add(key)
                tags.append(tag)
        return tags

    def validate_knowledgeBases(self, value):
        return self._owned_ids(KnowledgeBase, value, 'knowledge base')

    def validate_skills(self, value):
        return self._owned_ids(Skill, value, 'skill')

    def validate_delegatesTo(self, value):
        """Only agents the caller owns, checked the way knowledge bases are.

        Self-reference is dropped in `apply`, not here: the detail view
        validates a merged config without passing the row as `instance`, so
        `self.instance` is None on exactly the request where an agent could name
        itself.
        """
        return self._owned_ids(SubAgent, value, 'agent')

    def _owned_ids(self, model, ids, label):
        """Reject any id the caller does not own.

        Without this an agent could name someone else's knowledge base and read
        it on every run — the request is authenticated, so nothing downstream
        would look twice.
        """
        ids = sorted(set(ids))
        if not ids:
            return []
        user = self.context['request'].user
        owned = set(model.objects.filter(user=user, id__in=ids).values_list('id', flat=True))
        missing = [i for i in ids if i not in owned]
        if missing:
            raise serializers.ValidationError(
                f"No such {label}: {', '.join(map(str, missing))}."
            )
        return ids

    def validate(self, attrs):
        schedule = (attrs.get('schedule') or '').strip()
        tz = (attrs.get('scheduleTimezone') or 'UTC').strip() or 'UTC'
        if not zone_is_valid(tz):
            raise serializers.ValidationError(
                {'scheduleTimezone': f'"{tz}" is not an IANA timezone name, '
                                     f'e.g. "Asia/Kolkata".'}
            )
        attrs['scheduleTimezone'] = tz

        if schedule:
            if not cron_is_valid(schedule):
                raise serializers.ValidationError(
                    {'schedule': 'Expected five cron fields, e.g. "0 9 * * 1".'}
                )
            # Valid syntax is not the same as a schedule that ever comes round;
            # `0 0 30 2 *` parses and never fires. Caught here rather than left
            # as a NULL `next_due_at` nobody can interpret.
            if next_run_after(schedule, timezone.now(), tz) is None:
                raise serializers.ValidationError(
                    {'schedule': f'"{schedule}" has no next run — check the day '
                                 f'and month fields.'}
                )

        # Keyed off the schedule, not the trigger mode: `sync_schedule` creates
        # a Trigger for any non-blank cron, and the sweep calls the runtime with
        # `caller='trigger'`, which `_check_unattended` refuses unless this is
        # on. Same reasoning as the branch above — a schedule that cannot fire
        # is a silent no-op, except this one fails five times and then disables
        # the trigger, so the user loses even the evidence.
        if schedule and not attrs.get('allowUnattended', False):
            raise serializers.ValidationError(
                {'allowUnattended': 'A scheduled agent must be cleared to run '
                                    'unattended, otherwise every firing is refused. '
                                    'Turn it on, or clear the schedule.'}
            )

        # The shell-plus-open-egress refusal that stood here is gone with the
        # `egress` field: there is no longer a way to ask for network access, so
        # there is no longer a combination to refuse. The sandbox has none.

        return attrs

    # ---------------- mapping ----------------

    @staticmethod
    def to_config(agent: SubAgent) -> dict:
        """Model columns -> the flat AgentConfig the builder speaks."""
        sandbox = agent.sandbox or {}
        guards = agent.guardrails or {}
        ctx = agent.agent_context or {}
        settings_ = agent.runtime_settings or {}
        # The cron lives on a `Trigger` row, because the schedule sweep queries
        # `next_due_at` across every user. Only the *builder's* row round-trips
        # through this flat field: an agent may now carry several schedules, and
        # reading back whichever happened to sort first would let a save from
        # this screen overwrite one added on the Schedules page.
        triggers_ = [t for t in agent.triggers.all() if t.mode == 'schedule']
        schedule = next((t for t in triggers_ if t.origin == 'builder'), None)
        workflow = agent  # local alias; the remaining lines read column-wise
        return {
            'id': agent.id,
            'name': agent.name,
            'brief': agent.prompt or '',
            'description': agent.description or '',
            'tags': list(agent.tags or []),
            'provider': agent.llm_provider,
            'model': agent.llm_model,
            'temperature': settings_.get('temperature', 0.2),
            'effort': settings_.get('effort', ''),
            'fileAccess': sandbox.get('fileAccess', 'scoped'),
            'tools': {k: bool((workflow.tool_grants or {}).get(k, False)) for k in sorted(TOOL_KEYS)},
            'connectors': ctx.get('connectors', []),
            'knowledgeBases': ctx.get('knowledgeBases', []),
            'skills': ctx.get('skills', []),
            'delegatesTo': ctx.get('delegatesTo', []),
            'useEnvironment': ctx.get('useEnvironment', False),
            # The contract by name, blank for prose. `output_schema` is stored
            # as `{'contract': name}`; anything else in there is a shape from
            # before the registry closed and reads as prose, which is what
            # `contracts.resolve` already does with it.
            'outputContract': (
                (agent.output_schema or {}).get('contract', '')
                if contracts.resolve(agent.output_schema) else ''
            ),
            'fanoutParallel': (agent.fanout or {}).get('parallel'),
            # Derived, not stored: an agent with a schedule is a maintenance
            # agent, and that is the whole of what the retired `trigger` field
            # meant. Emitted read-only so the builder can still say how the
            # agent is invoked without owning a second answer.
            'trigger': 'maintenance' if schedule else 'goal',
            'schedule': schedule.cron if schedule else '',
            'scheduleTimezone': schedule.tz if schedule else 'UTC',
            # How many *other* schedules this agent has, so the builder can say
            # so and link out rather than pretending its one field is the whole
            # picture.
            'extraSchedules': max(len(triggers_) - (1 if schedule else 0), 0),
            'allowUnattended': agent.allow_unattended,
            'autonomy': guards.get('autonomy', 'ask'),
            'notifyOnHitl': guards.get('notifyOnHitl', True),
            'reviewAgent': guards.get('reviewAgent', False),
            'spendCapRupees': guards.get('spendCapRupees', 500),
            # Read through `budget` rather than straight off the dict: an agent
            # saved before this field existed has no key at all, and every one
            # of them still has to run.
            'maxRunSeconds': budget.limit_for(agent),
            'summaryModel': settings_.get('summaryModel', ''),
            'summaryProvider': settings_.get('summaryProvider', ''),
            'recursiveContext': settings_.get('recursiveContext', True),
            'compaction': settings_.get('compaction', True),
            'indexing': settings_.get('indexing', True),
            'status': workflow.status,
            'created_at': workflow.created_at,
            'updated_at': workflow.updated_at,
        }

    @staticmethod
    def apply(workflow: SubAgent, data: dict) -> SubAgent:
        """Validated AgentConfig -> model columns. Caller saves.

        The schedule is *not* applied here: it lives on a `Trigger` row, which
        needs the agent to have a primary key first. `sync_schedule` does it
        after the save.
        """
        workflow.name = data['name']
        workflow.prompt = data.get('brief', '')
        # PATCH merges, so an absent key must leave the stored value alone —
        # these three are the ones a partial save from a narrow screen (the
        # Schedules page, the run view) would otherwise blank.
        if 'description' in data:
            workflow.description = data['description']
        if 'tags' in data:
            workflow.tags = data['tags']
        if 'status' in data:
            workflow.status = data['status']
        workflow.llm_provider = data.get('provider', 'openrouter')
        workflow.llm_model = data.get('model', '')

        workflow.sandbox = {'fileAccess': data.get('fileAccess', 'scoped')}
        # Store the full closed set, not just what was sent: an absent key must
        # read as "denied", never as "unset and therefore whatever the runtime
        # defaults to".
        sent_tools = data.get('tools') or {}
        workflow.tool_grants = {k: bool(sent_tools.get(k, False)) for k in sorted(TOOL_KEYS)}

        workflow.agent_context = {
            'connectors': data.get('connectors', []),
            'knowledgeBases': data.get('knowledgeBases', []),
            'skills': data.get('skills', []),
            # An agent naming itself is dropped rather than refused: the
            # intent is obvious (delegate to the others), the depth limit would
            # stop the recursion anyway, and a toolbox offering an agent itself
            # is one the model wastes a turn on. Done here because this is the
            # only place the row's own id is in hand.
            'delegatesTo': [
                i for i in data.get('delegatesTo', []) if i != workflow.id
            ],
            'useEnvironment': data.get('useEnvironment', False),
        }

        # `{}` is prose, and prose is the default — so an unset contract clears
        # the column rather than leaving whatever was there. Same for fanout: a
        # blank field means one sequential turn, which is what an empty dict
        # already means to `run_fanout`.
        contract = (data.get('outputContract') or '').strip()
        workflow.output_schema = {'contract': contract} if contract else {}
        parallel = data.get('fanoutParallel')
        workflow.fanout = {'parallel': int(parallel)} if parallel else {}
        autonomy = data.get('autonomy', 'ask')
        workflow.guardrails = {
            'autonomy': autonomy,
            'notifyOnHitl': data.get('notifyOnHitl', True),
            'reviewAgent': data.get('reviewAgent', False),
            'spendCapRupees': data.get('spendCapRupees', 500),
            'maxRunSeconds': budget.clamp_run_seconds(
                data.get('maxRunSeconds', DEFAULT_RUN_SECONDS)
            ),
        }
        if 'allowUnattended' in data:
            workflow.allow_unattended = bool(data['allowUnattended'])

        settings_ = dict(workflow.runtime_settings or {})
        settings_.update({
            'temperature': data.get('temperature', 0.2),
            'effort': data.get('effort', ''),
            'summaryModel': data.get('summaryModel', ''),
            'summaryProvider': data.get('summaryProvider', ''),
            'recursiveContext': data.get('recursiveContext', True),
            'compaction': data.get('compaction', True),
            'indexing': data.get('indexing', True),
        })
        workflow.runtime_settings = settings_
        return workflow

    @staticmethod
    def sync_schedule(agent: SubAgent, data: dict) -> None:
        """Reconcile the *builder's* schedule Trigger with the submitted config.

        Called after save, because a Trigger needs the agent's primary key.
        A blank schedule removes the row rather than leaving a disabled one:
        a trigger that exists but never fires is the kind of state that shows
        up in a listing and makes someone wonder which of the two is true.

        Scoped to `origin='builder'`. The builder carries one cron field and
        reconciles it on every PATCH — and the builder PATCHes constantly — so
        an unscoped reconcile means every save from this screen deletes or
        overwrites a schedule the user added on the Schedules page. One field
        cannot be the source of truth for a list; it can only own its own row.

        Rows predating the column need no special case here: migration 0020
        marks every one of them `builder`, which is what they were — until it
        landed, `sync_schedule` was the only thing that created a schedule.
        Guessing at read time instead ("adopt it if it is the only one") would
        make an agent's single deliberately-manual schedule indistinguishable
        from a legacy one, and quietly overwrite it.
        """
        from agents.models import Trigger

        cron = (data.get('schedule') or '').strip()
        tz = (data.get('scheduleTimezone') or 'UTC').strip() or 'UTC'
        existing = agent.triggers.filter(mode='schedule', origin='builder').first()

        if not cron:
            if existing:
                existing.delete()
            return

        # Armed from the row's own start date where it has one, matching
        # `views/triggers._arm`, so the builder and the Schedules page cannot
        # arm the same schedule to two different instants.
        now = timezone.now()

        if existing:
            after = max(now, existing.starts_at) if existing.starts_at else now
            existing.config = {'cron': cron}
            existing.timezone = tz
            existing.origin = 'builder'
            existing.goal = agent.prompt or ''
            existing.next_due_at = next_run_after(cron, after, tz)
            existing.save(update_fields=[
                'config', 'timezone', 'origin', 'goal', 'next_due_at',
                'updated_at',
            ])
            return

        Trigger.objects.create(
            subagent=agent, mode='schedule', config={'cron': cron},
            timezone=tz, origin='builder', goal=agent.prompt or '',
            next_due_at=next_run_after(cron, now, tz),
        )


def _with_stats(configs, workflows, user):
    """Attach observed behaviour to serialized agents.

    These are the numbers that say whether delegating is paying off, so they are
    counted from ExecutionLog rather than stored on the agent — a stored counter
    drifts, a query cannot.
    """
    ids = [w.id for w in workflows]
    if not ids:
        return configs

    # Two queries, not one, and both counts are `distinct`. Filtering on
    # `hitl_requests` forces a LEFT JOIN, so a run with three HITL requests
    # contributes three rows — a plain Count reported it as three runs, and a
    # plain Sum multiplied its spend by three. `Sum` has no honest de-duplicated
    # form over that join, so the spend is aggregated separately, without it.
    rows = (
        ExecutionLog.objects
        .filter(user=user, subagent_id__in=ids)
        .values('subagent_id')
        .annotate(
            runs=Count('id', distinct=True),
            # A run nobody had to touch: no HITL request was raised against it.
            unattended=Count('id', filter=Q(hitl_requests__isnull=True),
                             distinct=True),
        )
    )
    # Spend goes through the same conversion the spend cap refuses runs on —
    # see agents/spend.py — so the number shown here and the number enforced
    # there cannot drift. Priced runs are summed from what they recorded;
    # unpriced ones fall back to the blended token rate rather than to zero.
    spend_rows = (
        ExecutionLog.objects
        .filter(user=user, subagent_id__in=ids)
        .values('subagent_id')
        .annotate(
            priced_usd=Sum('cost_usd', filter=Q(cost_source__in=PRICED_SOURCES)),
            unpriced_tokens=Sum(
                'tokens_used', filter=~Q(cost_source__in=PRICED_SOURCES)
            ),
        )
    )
    spend_by_id = {
        r['subagent_id']: rupees_for_usd(r['priced_usd']) + rupees_for(r['unpriced_tokens'])
        for r in spend_rows
    }

    by_id = {r['subagent_id']: r for r in rows}
    for cfg in configs:
        r = by_id.get(cfg['id'], {})
        cfg['runs'] = r.get('runs', 0)
        cfg['unattended'] = r.get('unattended', 0)
        cfg['spend'] = spend_by_id.get(cfg['id'], 0)
    return configs

@extend_schema(
    methods=['GET'],
    responses={200: AgentSerializer(many=True)},
    description='List the caller\'s agents, with run statistics.',
)
@extend_schema(
    methods=['POST'],
    request=AgentSerializer,
    responses={201: AgentSerializer},
    description='Create an agent from a knob-board configuration.',
)
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def agent_list(request):
    if request.method == 'GET':
        qs = (SubAgent.objects.filter(user=request.user)
              .prefetch_related('triggers').order_by('-updated_at'))
        workflows = list(qs)
        configs = [AgentSerializer.to_config(w) for w in workflows]
        return Response(_with_stats(configs, workflows, request.user))

    serializer = AgentSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)

    # `unique_together = (user, name)` backs the de-duplication below, so a
    # duplicate create gets a suffixed name rather than a 500.
    base_name = serializer.validated_data['name']
    name = base_name
    counter = 1
    while SubAgent.objects.filter(user=request.user, name=name).exists():
        name = f'{base_name} ({counter})'
        counter += 1

    data = dict(serializer.validated_data, name=name)
    agent = AgentSerializer.apply(SubAgent(user=request.user), data)
    agent.save()
    AgentSerializer.sync_schedule(agent, data)
    # Revision 1. Every later save diffs against this, so an agent with no
    # creation revision would show its first edit as though it invented the
    # whole configuration.
    revisions.record(agent, user=request.user, source='create')
    logger.info('Agent %s created by user %s', agent.id, request.user.id)
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0],
                    status=status.HTTP_201_CREATED)


@extend_schema(methods=['GET'], responses={200: AgentSerializer})
@extend_schema(methods=['PUT', 'PATCH'], request=AgentSerializer, responses={200: AgentSerializer})
@extend_schema(methods=['DELETE'], responses={204: OpenApiResponse(description='Agent deleted')})
@api_view(['GET', 'PUT', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def agent_detail(request, agent_id: int):
    agent = get_object_or_404(SubAgent, id=agent_id, user=request.user)

    if request.method == 'GET':
        return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])

    if request.method == 'DELETE':
        agent.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    # PATCH merges onto the current config so a partial save cannot silently
    # reset an unsent knob to its default — which, for a grant, would mean
    # quietly widening or narrowing what the agent may do.
    incoming = request.data
    if request.method == 'PATCH':
        current = AgentSerializer.to_config(agent)
        merged = {k: v for k, v in current.items()
                  if k not in ('id', 'status', 'created_at', 'updated_at')}
        merged.update(incoming)
        incoming = merged

    serializer = AgentSerializer(data=incoming, context={'request': request})
    serializer.is_valid(raise_exception=True)

    name = serializer.validated_data['name']
    if SubAgent.objects.filter(user=request.user, name=name).exclude(id=agent.id).exists():
        return Response({'error': 'You already have an agent with that name.'},
                        status=status.HTTP_400_BAD_REQUEST)

    AgentSerializer.apply(agent, serializer.validated_data).save()
    # Schedule first, *then* the revision. `to_config` reads the cron off the
    # agent's Trigger row, so recording before the sync snapshots the previous
    # schedule — every revision would show the edit before this one. `agent_list`
    # has always done it in this order; this path did not.
    AgentSerializer.sync_schedule(agent, serializer.validated_data)
    revisions.record(agent, user=request.user, source='update')
    return Response(_with_stats([AgentSerializer.to_config(agent)], [agent], request.user)[0])
