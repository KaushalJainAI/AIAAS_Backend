"""
`/api/eval/` — suites, cases, sweeps, and the review queue.

Views are thin: validate, call one function in `queries.py` or one in
`runner.py` / `supervision.py`, return. Everything is scoped to `request.user`
through the query layer rather than by trusting an id in the URL.

Only `suite_run` is async, for the same reason `agent_execute` is: it preflights
the provider before answering, so a suite pointed at an agent with no credential
is a 402 while the caller is still listening rather than a 202 followed by a
sweep that dies on its first case.
"""
import logging

from adrf.decorators import api_view as async_api_view
from django.db import transaction
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from workflow_backend.thresholds import EVAL_MAX_CASES_PER_SUITE

from . import graders, queries, supervision
from .models import EvalCase, EvalSuite
from .serializers import (
    EvalCaseSerializer,
    EvalResultSerializer,
    EvalRunSerializer,
    EvalSuiteSerializer,
    QueueFilterSerializer,
    ReviewInputSerializer,
    RunListFilterSerializer,
    RunRequestSerializer,
)

logger = logging.getLogger(__name__)


def _validated(serializer_class, request, *, source='query') -> dict:
    data = request.query_params if source == 'query' else request.data
    serializer = serializer_class(data=data)
    serializer.is_valid(raise_exception=True)
    return serializer.validated_data


def _delete_eval_execution_logs(user, execution_ids) -> None:
    """Delete only execution traces created by this user's eval sweeps."""
    from logs.models import ExecutionLog

    ids = [execution_id for execution_id in execution_ids if execution_id]
    if ids:
        ExecutionLog.objects.filter(
            id__in=ids, user=user, caller='eval',
        ).delete()


def _delete_eval_attempts(user, run, result_ids) -> None:
    from .environment import delete_attempts

    delete_attempts(user, run.suite_id, run.world_version, result_ids)


def _delete_eval_result_data(user, run, result) -> None:
    _delete_eval_execution_logs(user, [result.execution_id])
    _delete_eval_attempts(user, run, [result.pk])


def _delete_eval_run_data(user, run) -> None:
    result_refs = list(run.results.values_list('id', 'execution_id'))
    _delete_eval_execution_logs(
        user, (execution_id for _, execution_id in result_refs),
    )
    _delete_eval_attempts(user, run, (result_id for result_id, _ in result_refs))


# ======================== Graders ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def grader_catalog(request):
    """Every grader that can be put in a case, from the runner's own registry.

    Rendered by the case editor's picker. Served from `graders.REGISTRY` rather
    than from a list in the frontend, so a picker can never offer a grader the
    runner does not implement.
    """
    return Response({'graders': graders.catalog()})


# ======================== Suites ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def suite_list(request):
    """List the caller's suites, or create one."""
    if request.method == 'GET':
        suites = queries.suites_for(request.user)
        return Response({
            'suites': EvalSuiteSerializer(suites, many=True).data,
            'health': queries.suite_health(request.user),
        })

    serializer = EvalSuiteSerializer(data=request.data, context={'request': request})
    serializer.is_valid(raise_exception=True)
    suite = serializer.save(user=request.user)
    return Response(EvalSuiteSerializer(suite).data, status=status.HTTP_201_CREATED)


@extend_schema(
    methods=['GET', 'PATCH'], responses={200: OpenApiTypes.OBJECT},
)
@extend_schema(
    methods=['DELETE'],
    responses={
        204: OpenApiResponse(description='Suite and its evaluation history deleted.'),
        409: OpenApiTypes.OBJECT,
    },
)
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def suite_detail(request, suite_id: int):
    suite = get_object_or_404(EvalSuite, id=suite_id, user=request.user)

    if request.method == 'GET':
        return Response({
            **EvalSuiteSerializer(suite).data,
            'cases': EvalCaseSerializer(
                suite.cases.order_by('order', 'id'), many=True
            ).data,
        })

    if request.method == 'DELETE':
        from . import kb_world as _kb_worlds

        with transaction.atomic():
            runs = list(suite.runs.select_for_update())
            if any(
                run.status in ('pending', 'running')
                or (run.status == 'cancelled' and run.completed_at is None)
                for run in runs
            ):
                return Response(
                    {'error': 'Wait for active sweeps to finish stopping before deleting this suite.'},
                    status=status.HTTP_409_CONFLICT,
                )
            for run in runs:
                _delete_eval_run_data(request.user, run)
            from .environment import delete_suite_files

            delete_suite_files(request.user, suite.id)
            _kb_worlds.drop_suite_kbs(request.user, suite.id)
            suite.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = EvalSuiteSerializer(
        suite, data=request.data, partial=True, context={'request': request},
    )
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(EvalSuiteSerializer(suite).data)


# ======================== Cases ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET', 'POST'])
@permission_classes([IsAuthenticated])
def case_list(request, suite_id: int):
    suite = get_object_or_404(EvalSuite, id=suite_id, user=request.user)

    if request.method == 'GET':
        cases = suite.cases.order_by('order', 'id')
        return Response({'cases': EvalCaseSerializer(cases, many=True).data})

    # A cap on the suite, not on the request: a sweep runs the agent once per
    # case, so the size of a suite is the size of a bill.
    if suite.cases.count() >= EVAL_MAX_CASES_PER_SUITE:
        return Response(
            {'error': f'A suite holds at most {EVAL_MAX_CASES_PER_SUITE} cases.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    serializer = EvalCaseSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    from .environment import case_world_version
    case = serializer.save(suite=suite, world_version=case_world_version(suite))
    return Response(EvalCaseSerializer(case).data, status=status.HTTP_201_CREATED)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET', 'PATCH', 'DELETE'])
@permission_classes([IsAuthenticated])
def case_detail(request, case_id: int):
    case = get_object_or_404(EvalCase, id=case_id, suite__user=request.user)

    if request.method == 'GET':
        return Response(EvalCaseSerializer(case).data)

    if request.method == 'DELETE':
        # The rows that scored it survive: `EvalResult.case` is SET_NULL with
        # the goal copied, so deleting a case does not rewrite history.
        case.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)

    serializer = EvalCaseSerializer(case, data=request.data, partial=True)
    serializer.is_valid(raise_exception=True)
    serializer.save()
    return Response(EvalCaseSerializer(case).data)


# ======================== Sweeps ========================

@extend_schema(
    methods=['POST'],
    request=RunRequestSerializer,
    responses={202: OpenApiTypes.OBJECT},
    description='Sweep a suite against an agent. 202 + run_id; poll the run for progress.',
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def suite_run(request, suite_id: int):
    """Start a sweep and return its id at once.

    **202, not 200.** A sweep is one agent run per case; blocking the request
    until it finished would tie a browser to minutes of model calls. Guardrails
    and the provider credential are still resolved *before* answering, so a
    sweep that cannot be paid for is refused while the caller is listening.
    """
    from agents.models import SubAgent
    from llm import access as llm

    from agents.agent.runtime import AgentRunRefused
    from .runner import NoCasesToRun, start_suite_run

    suite = await EvalSuite.objects.filter(
        id=suite_id, user=request.user
    ).select_related('subagent').afirst()
    if suite is None:
        return Response({'error': 'Suite not found'}, status=status.HTTP_404_NOT_FOUND)

    body = RunRequestSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    agent_id = body.validated_data.get('agent_id') or suite.subagent_id
    if not agent_id:
        return Response(
            {'error': 'This suite names no agent. Pass agent_id, or set one on the suite.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    agent = await SubAgent.objects.filter(id=agent_id, user=request.user).afirst()
    if agent is None:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    try:
        run_id = await start_suite_run(
            suite, agent, request.user, notes=body.validated_data.get('notes', ''),
        )
    except NoCasesToRun as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except AgentRunRefused as exc:
        # The agent's budget is spent. 402, not 403: the caller is permitted.
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except llm.LLMAccountError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except llm.LLMUnavailable as exc:
        # A retired model or a provider slug with no handler. Nothing is owed,
        # so 400 rather than 402 — the same split `agent_execute` makes.
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception('[Eval] suite %s failed to start', suite_id)
        return Response({'error': 'The evaluation could not be started.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    return Response(
        {'run_id': run_id, 'suite_id': suite.id, 'agent_id': agent.id,
         'supervision': suite.supervision},
        status=status.HTTP_202_ACCEPTED,
    )


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def run_list(request):
    """Sweep history, newest first."""
    params = _validated(RunListFilterSerializer, request)
    rows, meta = queries.run_page(
        request.user,
        limit=params['limit'],
        suite_id=params.get('suite_id'),
        agent_id=params.get('agent_id'),
        status=params.get('status'),
    )
    return Response({'runs': EvalRunSerializer(rows, many=True).data, **meta})


@extend_schema(methods=['GET'], responses={200: OpenApiTypes.OBJECT})
@extend_schema(
    methods=['DELETE'],
    responses={
        204: OpenApiResponse(description='Sweep, eval-only traces and hidden attempt files deleted.'),
        409: OpenApiTypes.OBJECT,
    },
    description='Delete a finished sweep, its eval-only execution traces and hidden attempt files.',
)
@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id: str):
    """Read a sweep and results, or delete the finished sweep and its artifacts."""
    run, results, meta = queries.run_with_results(request.user, run_id)
    if run is None:
        return Response({'error': 'Run not found'}, status=status.HTTP_404_NOT_FOUND)
    if request.method == 'DELETE':
        if (run.status in ('pending', 'running')
                or (run.status == 'cancelled' and run.completed_at is None)):
            return Response(
                {'error': 'Wait for the sweep to finish stopping before deleting it.'},
                status=status.HTTP_409_CONFLICT,
            )
        # Clear a junk sweep and the space it held. Eval `ExecutionLog`s are
        # hidden from stats and `/runs` (`caller='eval'`), so leaving them
        # behind would free nothing the user can see while keeping every
        # turn/step row. Only the caller's own eval logs are touched.
        with transaction.atomic():
            _delete_eval_run_data(request.user, run)
            run.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    return Response({
        **EvalRunSerializer(run).data,
        'results': EvalResultSerializer(results, many=True).data,
        **meta,
    })


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def run_cancel(request, run_id: str):
    """Stop a sweep at the next case boundary.

    Cooperative: the sweep checks this row as each case leaves the concurrency
    queue. A case already inside a model call runs to completion rather than
    being abandoned half-paid-for.
    """
    run, _, _ = queries.run_with_results(request.user, run_id)
    if run is None:
        return Response({'error': 'Run not found'}, status=status.HTTP_404_NOT_FOUND)
    if run.is_complete:
        return Response({'error': f'This run already {run.status}.'},
                        status=status.HTTP_400_BAD_REQUEST)

    run.status = 'cancelled'
    run.save(update_fields=['status', 'updated_at'])
    return Response({'run_id': str(run.run_id), 'status': run.status})


# ======================== Supervision ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def review_queue(request):
    """Results waiting on this person, oldest first."""
    params = _validated(QueueFilterSerializer, request)
    rows, meta = queries.review_queue(
        request.user,
        limit=params['limit'],
        suite_id=params.get('suite_id'),
        run_id=params.get('run_id') or None,
    )
    return Response({
        'queue': [
            {
                **EvalResultSerializer(row).data,
                'suite_id': row.run.suite_id,
                'suite_name': row.run.suite.name,
                'run_id': str(row.run.run_id),
                'agent_name': row.run.subagent.name if row.run.subagent_id else '',
                # The rubric the reviewer is meant to apply. Sent with the
                # queue rather than fetched per row: a reviewer deciding
                # without the reference is guessing.
                'reference': row.case.reference if row.case_id else '',
            }
            for row in rows
        ],
        **meta,
    })


@extend_schema(
    methods=['POST'], request=ReviewInputSerializer, responses={200: OpenApiTypes.OBJECT},
    description=(
        "Record a verdict on a graded result; `unsure` dismisses a legacy errored "
        "or skipped result and re-settles its run."
    ),
)
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def submit_review(request, result_id: int):
    """Record a verdict.

    The verdict overrides the graders for scoring but never overwrites them —
    `EvalReview.agreed_with_graders` is computed here and is what
    `EvalRun.grader_agreement` is built from. Re-posting is an edit, not a
    second opinion.
    """
    result = queries.reviewable_result(request.user, result_id)
    if result is None:
        return Response({'error': 'Result not found'}, status=status.HTTP_404_NOT_FOUND)
    body = _validated(ReviewInputSerializer, request, source='body')
    if result.status in ('error', 'skipped'):
        # The agent never answered (or never ran), so there is no verdict to
        # record. Older sweeps queued these before `apply_policy` stopped
        # doing so. Dismissing it (the UI sends `unsure`) deletes the row and
        # its eval-only trace, then re-settles the run instead of returning a
        # 400 for a result with no answer to judge.
        run = result.run
        with transaction.atomic():
            _delete_eval_result_data(request.user, run, result)
            result.delete()
            supervision.recompute(run)
        run.refresh_from_db()
        return Response({
            'deleted': True,
            'run': EvalRunSerializer(run).data,
        })

    review = supervision.record_review(
        result,
        reviewer=request.user,
        verdict=body['verdict'],
        comment=body.get('comment', ''),
        corrected_answer=body.get('corrected_answer', ''),
    )
    result.refresh_from_db()
    result.run.refresh_from_db()
    return Response({
        'result': EvalResultSerializer(result).data,
        'run': EvalRunSerializer(result.run).data,
        'agreed_with_graders': review.agreed_with_graders,
    })


# ======================== Judge calibration ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def judge_calibration(request):
    """Latest judge calibration per source (platform-wide, read-only)."""
    from .models import JudgeCalibration

    source = request.query_params.get('source', '')
    qs = JudgeCalibration.objects.order_by('-created_at')
    if source in ('handwritten', 'gold'):
        qs = qs.filter(source=source)
    latest: dict[str, dict] = {}
    for row in qs[:20]:
        if row.source not in latest:
            latest[row.source] = {
                'judge_provider': row.judge_provider,
                'judge_model': row.judge_model,
                'n': row.n,
                'agreement': row.agreement,
                'false_pass_rate': row.false_pass_rate,
                'false_fail_rate': row.false_fail_rate,
                'source': row.source,
                'created_at': row.created_at,
            }
    return Response({'calibrations': latest})


class CaseFromRunSerializer(__import__('rest_framework').serializers.Serializer):
    execution_id = __import__('rest_framework').serializers.CharField()
    suite_id = __import__('rest_framework').serializers.IntegerField(
        required=False, allow_null=True)


@extend_schema(methods=['POST'], request=CaseFromRunSerializer,
               responses={201: OpenApiTypes.OBJECT},
               description='Save a run as an eval case for the same agent.')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def case_from_run(request):
    """A bad run becomes a test — as a draft.

    `graders = []` is deliberate: a case with no graders is queued under every
    policy, which is right for a success criterion nobody has written yet.
    Files are v1-limited: paths the run read are recorded in `reference`, but
    no `__workspace__` is built — current contents may differ from what the
    run saw, and a silently differing fixture is worse than none.

    Draft, not active: the goal and reference are the run's own words, so a
    person accepts the test on the Evals page before it scores anything.
    """
    from logs.models import ExecutionLog

    from . import api as _api
    from .models import EvalSuite
    from .serializers import EvalCaseSerializer

    body = CaseFromRunSerializer(data=request.data)
    body.is_valid(raise_exception=True)
    execution_id = body.validated_data['execution_id']
    try:
        log = ExecutionLog.objects.select_related('subagent').filter(
            execution_id=execution_id, user=request.user).first()
    except Exception:  # noqa: BLE001
        log = None
    if log is None:
        return Response({'error': 'Run not found'}, status=status.HTTP_404_NOT_FOUND)
    if log.subagent_id is None:
        return Response(
            {'error': 'Chat turns have no agent to replay against.'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    suite_id = body.validated_data.get('suite_id')
    suite = None
    if suite_id:
        suite = EvalSuite.objects.filter(id=suite_id, user=request.user).first()
        if suite is None:
            return Response({'error': 'Suite not found'}, status=status.HTTP_404_NOT_FOUND)
    else:
        suite, _ = EvalSuite.objects.get_or_create(
            user=request.user, name='From runs',
            defaults={
                'description': 'Cases saved from real runs.',
                'subagent': log.subagent,
                'supervision': 'all',
            },
        )
    payload = dict(log.input_data or {})
    payload.pop('thread_id', None)
    for key in [k for k in payload if str(k).startswith('_')]:
        payload.pop(key, None)
    goal = str(payload.pop('goal', '') or '')
    # Feedback comment becomes the reference — the reviewer's words about good.
    reference = ''
    try:
        fb = log.feedbacks.filter(user=request.user).first()
        reference = (fb.comment or '') if fb else ''
    except Exception:  # noqa: BLE001
        reference = ''
    # Paths the run read, recorded — not built into a workspace (v1 limit).
    read_paths: list[str] = []
    try:
        for call in (log.output_data or {}).get('tool_trace', []) or []:
            name = str(call.get('tool') or call.get('name') or '')
            if name in ('read_file', 'list_files'):
                args = call.get('args') or call.get('arguments') or {}
                if isinstance(args, dict) and args.get('path'):
                    read_paths.append(str(args['path']))
    except Exception:  # noqa: BLE001
        read_paths = []
    if read_paths and not reference:
        reference = 'Files this run read: ' + ', '.join(sorted(set(read_paths)))
    elif read_paths:
        reference = (reference + '\nFiles this run read: '
                     + ', '.join(sorted(set(read_paths)))).strip()
    saved = _api.save_cases(suite, [{
        'name': f'From run {str(log.execution_id)[:8]}',
        'goal': goal, 'input_data': payload, 'reference': reference,
        'graders': [], 'tags': ['from-run', str(log.execution_id)],
    }], drafts=True)
    if not saved:
        return Response(
            {'error': 'The suite is full.'}, status=status.HTTP_400_BAD_REQUEST)
    return Response({**EvalCaseSerializer(saved[0]).data, 'draft': True},
                    status=status.HTTP_201_CREATED)


# ======================== Starter kits (user datasets) ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def starter_kits(request):
    """Starter datasets a user can clone for their own agent.

    Query `?agent_id=` adds a `recommended` list derived from that agent's
    grants (`eval/starter_kits.py::recommended_kits`). No rows are created.
    """
    from . import starter_kits as _kits

    agent_id = request.query_params.get('agent_id')
    recommended: list[str] = []
    if agent_id:
        from agents.models import SubAgent
        try:
            agent = SubAgent.objects.filter(id=agent_id, user=request.user).first()
            if agent is not None:
                recommended = _kits.recommended_kits(agent.tool_grants or {})
        except Exception:  # noqa: BLE001
            recommended = []
    return Response({
        'kits': [
            {'slug': slug, 'name': kit['name'], 'description': kit['description'],
             'case_count': len(kit['cases'])}
            for slug, kit in _kits.STARTER_KITS.items()
        ],
        'recommended': recommended,
    })


@extend_schema(methods=['POST'], responses={201: OpenApiTypes.OBJECT},
               description='Clone a starter kit into a new suite for this user.')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def suite_from_template(request):
    """Clone a starter kit: one suite + its cases, owned by the caller.

    Body: `{"template": "research", "name": "...", "agent_id": 12?}`.
    Cases are validated through the same `validate_case_graders` the case
    editor uses, so a kit that drifted from the registry 400s instead of
    installing a suite that can never pass.
    """
    from . import starter_kits as _kits
    from .serializers import EvalCaseSerializer

    template = str((request.data or {}).get('template', '')).strip().lower()
    kit = _kits.get_kit(template)
    if kit is None:
        return Response(
            {'error': f'No such starter kit {template!r}. Known: {", ".join(sorted(_kits.STARTER_KITS))}'},
            status=status.HTTP_400_BAD_REQUEST,
        )
    agent = None
    agent_id = (request.data or {}).get('agent_id')
    if agent_id:
        from agents.models import SubAgent
        agent = SubAgent.objects.filter(id=agent_id, user=request.user).first()
        if agent is None:
            return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    from . import api as _api
    from . import graders as _graders
    try:
        suite = _api.clone_starter_kit(
            user=request.user,
            template=template,
            name=str((request.data or {}).get('name') or kit['name']),
            agent=agent,
        )
    except _graders.GraderError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    from .serializers import EvalSuiteSerializer
    return Response({
        **EvalSuiteSerializer(suite).data,
        'cases': EvalCaseSerializer(suite.cases.order_by('order', 'id'), many=True).data,
    }, status=status.HTTP_201_CREATED)


# ======================== Scorecard ========================

@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def agent_scorecard(request, agent_id: int):
    """How one agent scores across every suite pointed at it."""
    from agents.models import SubAgent

    agent = get_object_or_404(SubAgent, id=agent_id, user=request.user)
    return Response({
        'agent_id': agent.id,
        'agent_name': agent.name,
        'suites': queries.agent_scorecard(request.user, agent.id),
    })


# ------------------------------------------------------------ generated data
#
# Test data the owner did not have to write (`eval/generator.py`). Both sources
# save **drafts** — `is_active=False`, tagged `needs-review` — which the runner
# skips, so nothing a model wrote is scored until a person accepts it.

def _save_drafts(suite, drafts: list[dict]) -> list:
    from . import api as _api

    tagged = []
    for draft in drafts:
        tags = ['generated', 'needs-review', draft.get('category', '')]
        if draft.get('execution_id'):
            tags = ['from-run', 'needs-review',
                    draft['execution_id'], draft.get('source', '')]
        tagged.append({**draft, 'tags': [t for t in tags if t]})
    return _api.save_cases(suite, tagged, drafts=True)


@extend_schema(responses={201: OpenApiTypes.OBJECT},
               description="Draft cases from the suite agent's own configuration.")
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def suite_generate(request, suite_id: int):
    """Body: `{"count": 12, "focus": "..."}`. Drafts land for review, unscored.

    The judge model writes them, billed to the caller's key like a judge call.
    A missing credential answers 402 naming the fix, not an empty list.
    """
    from asgiref.sync import sync_to_async

    from llm.access import LLMUserActionable

    from .generator import generate_cases

    suite = await EvalSuite.objects.select_related('subagent').filter(
        id=suite_id, user=request.user).afirst()
    if suite is None:
        return Response({'error': 'Suite not found'}, status=status.HTTP_404_NOT_FOUND)
    if suite.subagent is None:
        return Response({'error': 'Pick the agent this suite tests first.'},
                        status=status.HTTP_400_BAD_REQUEST)
    from .environment import live_world as _live_world

    if await sync_to_async(_live_world)(suite) is not None:
        return Response(
            {'error': 'This suite has an accepted world — generate cases from '
                      'the world instead (POST suites/<id>/world/generate/).'},
            status=status.HTTP_400_BAD_REQUEST)
    try:
        count = int(request.data.get('count') or 12)
    except (TypeError, ValueError):
        return Response({'error': 'count must be a number'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        out = await generate_cases(suite.subagent, user_id=request.user.id, count=count,
                                   focus=str(request.data.get('focus') or ''))
    except LLMUserActionable as exc:
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except ValueError as exc:
        return Response({'error': f'The generator reply could not be used: {exc}'},
                        status=status.HTTP_502_BAD_GATEWAY)
    except Exception as exc:  # noqa: BLE001 - provider down
        logger.warning('[Eval] generation failed: %s', exc)
        return Response({'error': f'Generation failed: {exc}'},
                        status=status.HTTP_502_BAD_GATEWAY)

    saved = await sync_to_async(_save_drafts)(suite, out['cases'])
    data = await sync_to_async(lambda: EvalCaseSerializer(saved, many=True).data)()
    return Response({
        'cases': data,
        'rejected': out['rejected'], 'tokens': out['tokens'],
        'cost_usd': out['cost_usd'], 'model': out['model'],
    }, status=status.HTTP_201_CREATED)


@extend_schema(responses={201: OpenApiTypes.OBJECT},
               description="Draft cases from the suite agent's recent real runs.")
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def suite_import_runs(request, suite_id: int):
    """Body: `{"source": "all" | "rated" | "thumbs_down", "limit": 20}`.

    Newest first, eval runs excluded (a case built from a test is a test of
    the test), and a run already imported into this suite is skipped.
    """
    from .generator import import_candidates

    suite = get_object_or_404(EvalSuite, id=suite_id, user=request.user)
    if suite.subagent_id is None:
        return Response({'error': 'Pick the agent this suite tests first.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        drafts, skipped = import_candidates(
            request.user, suite,
            source=str(request.data.get('source') or 'all'),
            limit=request.data.get('limit') or 20)
    except ValueError as exc:
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    saved = _save_drafts(suite, drafts)
    return Response({
        'cases': EvalCaseSerializer(saved, many=True).data,
        'already_imported': skipped,
    }, status=status.HTTP_201_CREATED)


@extend_schema(responses={200: OpenApiTypes.OBJECT},
               description='Accept or reject draft cases in one call.')
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def suite_review_drafts(request, suite_id: int):
    """Body: `{"accept": [ids], "reject": [ids]}`. Drafts only.

    Accepting activates the case and drops `needs-review`; rejecting deletes
    it. Ids that are not drafts of this suite are ignored rather than acted
    on, so this can never delete a case someone wrote by hand.
    """
    from .generator import DRAFT_TAG

    suite = get_object_or_404(EvalSuite, id=suite_id, user=request.user)

    def ids(key):
        try:
            return {int(i) for i in (request.data.get(key) or [])}
        except (TypeError, ValueError):
            return set()

    drafts = {c.id: c for c in suite.cases.filter(is_active=False)
              if DRAFT_TAG in (c.tags or [])}
    accept = ids('accept') & drafts.keys()
    # A case cannot be accepted before its world is: generations are
    # provisional until the situation they describe has been reviewed too.
    # World-less cases (hand-written, starter kits, from-run imports) are
    # unaffected — they belong to no version.
    from .environment import live_world

    live_version = None
    try:
        live = live_world(suite)
        live_version = live.version if live is not None else None
    except Exception:  # noqa: BLE001
        live = None
    refused = []
    for case_id in sorted(accept):
        case = drafts[case_id]
        if case.world_version is not None and case.world_version != live_version:
            refused.append(case_id)
    accept -= set(refused)
    for case_id in accept:
        case = drafts[case_id]
        case.is_active = True
        case.tags = [t for t in case.tags if t != DRAFT_TAG]
        case.save(update_fields=['is_active', 'tags', 'updated_at'])
    reject = (ids('reject') - ids('accept')) & drafts.keys()
    EvalCase.objects.filter(id__in=reject).delete()
    body: dict = {'accepted': len(accept), 'rejected': len(reject)}
    if refused:
        body['refused'] = refused
        body['refused_reason'] = (
            'these cases belong to a world version that is not accepted — '
            'accept the world first')
    return Response(body)


# ======================== Worlds ========================
#
# A world is generated (judge-built fixtures + cases), never hand-written, and
# accepted on the Evals page only. Generating always mints a new version, so
# regenerating never edits the world old sweeps ran on.


def _world_payload(world) -> dict:
    from .serializers import EvalWorldSerializer

    return EvalWorldSerializer(world).data


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def suite_world(request, suite_id: int):
    """The suite's live (accepted) world and its newest draft, if any."""
    from .environment import live_world

    suite = get_object_or_404(EvalSuite, id=suite_id, user=request.user)
    live = live_world(suite)
    draft = (suite.worlds.filter(status='draft').order_by('-version').first())
    return Response({
        'live': _world_payload(live) if live is not None else None,
        'draft': _world_payload(draft) if draft is not None else None,
        'versions': list(suite.worlds.order_by('-version')
                         .values_list('version', flat=True)),
    })


@extend_schema(responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT})
@api_view(['GET', 'DELETE'])
@permission_classes([IsAuthenticated])
def world_detail(request, world_id: int):
    """One world. Drafts may be deleted; accepted worlds are history and the
    endpoint refuses rather than rewriting what old sweeps ran on."""
    from .models import EvalWorld

    world = get_object_or_404(EvalWorld, id=world_id, suite__user=request.user)
    if request.method == 'GET':
        return Response(_world_payload(world))
    if world.status != 'draft':
        return Response(
            {'error': 'Accepted worlds are kept as history. Regenerate instead.'},
            status=status.HTTP_400_BAD_REQUEST)
    world.delete()
    return Response(status=status.HTTP_204_NO_CONTENT)


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def world_accept(request, world_id: int):
    """Accept a draft world. Its cases become acceptable; older versions'
    cases go stale (kept, listed, never swept).

    Accepting also drops older versions' hidden KBs: their cases are stale,
    so nothing will read them again, and a corpus nobody reads is storage
    masquerading as data. Attempt files under `/.eval/` are left alone —
    they are bounded per attempt and owned by their runs' history.
    """
    from . import kb_world as _kb_worlds
    from .models import EvalWorld

    world = get_object_or_404(EvalWorld, id=world_id, suite__user=request.user)
    if world.status != 'draft':
        return Response({'error': 'This world is already accepted.'},
                        status=status.HTTP_400_BAD_REQUEST)
    world.status = 'accepted'
    world.save(update_fields=['status', 'updated_at'])
    for old in world.suite.worlds.exclude(pk=world.pk).values_list('version', flat=True):
        _kb_worlds.drop_world_kb(request.user, world.suite_id, old)
    return Response(_world_payload(world))


@extend_schema(responses={201: OpenApiTypes.OBJECT},
               description="Judge-build a world and its cases. Drafts only.")
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def suite_world_generate(request, suite_id: int):
    """Body: `{"focus": "...", "cases": 12}`. Mints a new world version plus
    case drafts built for it — nothing is accepted and nothing scores until
    the Evals page says so.

    The judge calls are billed to the caller's key like any judge call: a
    missing credential answers 402 naming the fix, not an empty world.
    """
    from asgiref.sync import sync_to_async

    from llm.access import LLMUserActionable

    from .generator import generate_world

    suite = await EvalSuite.objects.select_related('subagent').filter(
        id=suite_id, user=request.user).afirst()
    if suite is None:
        return Response({'error': 'Suite not found'}, status=status.HTTP_404_NOT_FOUND)
    if suite.subagent is None:
        return Response({'error': 'Pick the agent this suite tests first.'},
                        status=status.HTTP_400_BAD_REQUEST)
    try:
        count = int(request.data.get('count') or request.data.get('cases') or 12)
    except (TypeError, ValueError):
        return Response({'error': 'cases must be a number'}, status=status.HTTP_400_BAD_REQUEST)
    try:
        out = await generate_world(
            suite.subagent, user_id=request.user.id,
            focus=str(request.data.get('focus') or ''), cases=count)
    except LLMUserActionable as exc:
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except ValueError as exc:
        return Response({'error': f'The generator reply could not be used: {exc}'},
                        status=status.HTTP_502_BAD_GATEWAY)
    except Exception as exc:  # noqa: BLE001 - provider down
        logger.warning('[Eval] world generation failed: %s', exc)
        return Response({'error': f'Generation failed: {exc}'},
                        status=status.HTTP_502_BAD_GATEWAY)

    def save():
        from . import api as _api

        return _api.save_generated_world(suite, out)

    world, saved = await sync_to_async(save)()
    data = await sync_to_async(lambda: EvalCaseSerializer(saved, many=True).data)()
    return Response({
        'world': await sync_to_async(_world_payload)(world),
        'cases': data,
        'rejected': out['rejected'], 'tokens': out['tokens'],
        'cost_usd': out['cost_usd'], 'model': out['model'],
    }, status=status.HTTP_201_CREATED)
