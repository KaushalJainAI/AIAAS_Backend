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
from django.shortcuts import get_object_or_404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
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


@extend_schema(responses={200: OpenApiTypes.OBJECT})
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
    case = serializer.save(suite=suite)
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


@extend_schema(responses={200: OpenApiTypes.OBJECT})
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def run_detail(request, run_id: str):
    """One sweep with every result, each carrying its grades and any review."""
    run, results, meta = queries.run_with_results(request.user, run_id)
    if run is None:
        return Response({'error': 'Run not found'}, status=status.HTTP_404_NOT_FOUND)
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
    description="Record a human verdict on one result and re-settle its run.",
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
    if result.status == 'error':
        # There is no answer to have an opinion about; the agent never
        # produced one. Refused rather than recorded, so a run's agreement
        # figure is never diluted by verdicts on outages.
        return Response(
            {'error': 'This case errored before the agent answered; there is nothing to review.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    body = _validated(ReviewInputSerializer, request, source='body')
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
