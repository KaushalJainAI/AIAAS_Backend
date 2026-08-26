"""
Run control: starting an agent run, and the three ways a human intervenes in
one that is already going.

Separated from agent CRUD because these share a mechanism CRUD has none of:
every one of them is async, every one reaches into a *live* run through the
LangGraph checkpointer, and every one is about a run's lifecycle rather than an
agent's configuration. Approve and reject are deliberate mirrors — before
`agent_reject` existed, a declined call left the run paused for ever.

`start_agent_run` is imported inside the views, not at module scope: the
runtime imports the model layer, and a module-level import here would make the
URLconf drag the whole agent runtime in at startup.
"""
import logging

from adrf.decorators import api_view as async_api_view
from drf_spectacular.utils import OpenApiResponse, extend_schema
from rest_framework import serializers, status
from rest_framework.decorators import permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from agents.models import SubAgent
from logs.models import ExecutionLog

logger = logging.getLogger(__name__)


class AgentExecuteSerializer(serializers.Serializer):
    """What one run is asked to do."""

    goal = serializers.CharField(max_length=8000)
    #: Pass the thread id of a paused run to resume it after approval.
    thread_id = serializers.CharField(required=False, allow_blank=True, default='')

    def validate_goal(self, value):
        goal = value.strip()
        if not goal:
            raise serializers.ValidationError('Give the agent something to do.')
        return goal


class AgentApproveSerializer(serializers.Serializer):
    thread_id = serializers.CharField(max_length=200)
    call_id = serializers.CharField(max_length=200)
    #: "and stop asking me about this tool" — stores a standing allowance for
    #: the tool this call was for, across every future run and conversation.
    remember = serializers.BooleanField(required=False, default=False)


class AgentRejectSerializer(serializers.Serializer):
    thread_id = serializers.CharField(max_length=200)
    call_id = serializers.CharField(max_length=200)
    #: Passed to the model verbatim, so it can adapt rather than guess why. A
    #: rejection with no reason still resumes the run; it just tells the model
    #: less.
    reason = serializers.CharField(max_length=500, required=False,
                                   allow_blank=True, default='')


@extend_schema(
    methods=['POST'],
    request=AgentExecuteSerializer,
    responses={202: OpenApiResponse(description='Run accepted; subscribe to the execution id')},
    description='Start an agent run against a goal, using only the tools it was granted.',
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def agent_execute(request, agent_id: int):
    """Start an agent run and return its execution id.

    **202, not 200.** The run now streams: every tool call is broadcast to
    `ws/execution/{execution_id}/` in the frame shapes the canvas renders.
    Blocking here until the run finished would make that stream
    pointless — nobody can subscribe to an id they have not been given yet.

    Guardrails and the provider credential are still checked *before*
    responding, so a run that cannot be paid for is refused while the caller is
    listening rather than dying quietly in the background a moment later.
    """
    from llm import access as llm

    from agents.agent.runtime import (
        AgentRunRefused, resume_agent_run, start_agent_run,
    )

    agent = await SubAgent.objects.filter(
        id=agent_id, user=request.user
    ).afirst()
    if agent is None:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AgentExecuteSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    thread_id = serializer.validated_data.get('thread_id') or None

    try:
        # A thread id names a run that already exists, so it has to *resume*
        # that run rather than start a second one against the same checkpointer
        # key. `start_agent_run` always opens a fresh `ExecutionLog`, which
        # split one run's trace across two execution ids the canvas cannot join
        # — the exact thing `resume_agent_run` exists to prevent. Falls through
        # to a normal start when no paused run answers to the id, so a stale
        # thread id is still a working request rather than a 404.
        execution_id = None
        if thread_id:
            execution_id = await resume_agent_run(
                agent, user=request.user, thread_id=thread_id,
            )
        if execution_id is None:
            execution_id = await start_agent_run(
                agent, serializer.validated_data['goal'], user=request.user,
                thread_id=thread_id,
            )
    except AgentRunRefused as exc:
        # A guardrail said no before anything ran. 402 rather than 403: the
        # caller is permitted, the agent's budget is spent.
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except llm.LLMAccountError as exc:
        # No credential for this agent's provider, a rejected key, or no credit
        # left. Same 402 for the same reason: permitted caller, uncallable
        # provider. The message names the provider and what to add, so it is
        # returned rather than swallowed into the generic 500 below, which
        # told the user only that the run "could not be started".
        return Response({'error': str(exc)}, status=status.HTTP_402_PAYMENT_REQUIRED)
    except llm.LLMUnavailable as exc:
        # A stale provider slug with no handler behind it, or a model the
        # provider has retired out from under a saved agent. Nothing to pay for
        # in either case, so 400 rather than 402.
        return Response({'error': str(exc)}, status=status.HTTP_400_BAD_REQUEST)
    except Exception:
        logger.exception('Agent %s failed to start', agent.id)
        return Response({'error': 'The agent run could not be started.'},
                        status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    from agents.agent.runtime import AgentToolbox

    return Response({
        'execution_id': execution_id,
        'status': 'running',
        # Named explicitly so the caller can tell the user a configured
        # capability was not honoured, rather than leaving them to infer it from
        # an agent that quietly cannot do its job. Available up front because it
        # is derived from the grants, not from the run.
        'unserved_grants': list(AgentToolbox.for_agent(agent, request.user.id).unserved),
    }, status=status.HTTP_202_ACCEPTED)


@extend_schema(
    methods=['POST'],
    request=AgentApproveSerializer,
    responses={200: OpenApiResponse(description='Approval recorded; resume the run')},
    description='Approve a tool call an agent run paused on.',
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def agent_approve(request, agent_id: int):
    """Record approval for a paused tool call and continue the run.

    Approving used to only write consent into the checkpoint. The paused run
    had already returned by then, so nothing picked it up again and the user
    approved into silence — the run simply stayed paused. Recording and
    resuming belong in one call because a user who approves has asked for the
    work to continue, not for permission to be filed.

    Ownership of the agent is re-checked here even though the pause carries a
    thread id: a thread id is a guess-resistant string, not an authorisation.
    """
    from chat.turn.agent import approve_tool_call

    from agents.agent.runtime import resume_agent_run

    agent = await SubAgent.objects.filter(
        id=agent_id, user=request.user
    ).afirst()
    if agent is None:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AgentApproveSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    thread_id = serializer.validated_data['thread_id']

    await approve_tool_call(
        thread_id, serializer.validated_data['call_id'],
        remember=serializer.validated_data.get('remember', False),
        user_id=request.user.id,
    )
    execution_id = await resume_agent_run(agent, user=request.user, thread_id=thread_id)

    return Response({'approved': True, 'execution_id': execution_id,
                     'resumed': execution_id is not None})


@extend_schema(
    methods=['POST'],
    request=AgentRejectSerializer,
    responses={200: OpenApiResponse(description='Rejection recorded; run resumed past the call')},
    description='Decline a tool call an agent run paused on.',
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def agent_reject(request, agent_id: int):
    """Decline a paused tool call and continue the run without it.

    The mirror of `agent_approve`, and it exists because the asymmetry was a
    bug: approving resumed the run, declining did nothing, so a declined call
    left the run paused for ever — holding a `HITLRequest` that went on nudging
    the user about a step that would never happen. The model is told what was
    refused and carries on with what it can still do.
    """
    from chat.turn.agent import reject_tool_call

    from agents.agent.runtime import resume_agent_run

    agent = await SubAgent.objects.filter(
        id=agent_id, user=request.user
    ).afirst()
    if agent is None:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AgentRejectSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    thread_id = serializer.validated_data['thread_id']

    recorded = await reject_tool_call(
        thread_id, serializer.validated_data['call_id'],
        reason=serializer.validated_data.get('reason', ''),
    )
    if not recorded:
        return Response({'error': 'No paused run for that thread'},
                        status=status.HTTP_404_NOT_FOUND)

    execution_id = await resume_agent_run(agent, user=request.user, thread_id=thread_id)

    return Response({'rejected': True, 'execution_id': execution_id,
                     'resumed': execution_id is not None})


class AgentSteerSerializer(serializers.Serializer):
    message = serializers.CharField(max_length=4000)

    def validate_message(self, value):
        message = value.strip()
        if not message:
            raise serializers.ValidationError('Say something to steer with.')
        return message


@extend_schema(
    methods=['POST'],
    request=AgentSteerSerializer,
    responses={200: OpenApiResponse(description='Steer delivered to the running turn')},
    description='Send a mid-run instruction to an agent run that is already going.',
)
@async_api_view(['POST'])
@permission_classes([IsAuthenticated])
async def agent_steer(request, agent_id: int):
    """Say something to a run in flight, without restarting it.

    The alternative — stop and start again with a fuller goal — throws away
    every tool result the run has already paid for, and splits one piece of
    work across two execution logs the canvas cannot join. This lands in the
    same run, on the same log, in the same stream.
    """
    from chat.turn import steering

    agent = await SubAgent.objects.filter(
        id=agent_id, user=request.user
    ).afirst()
    if agent is None:
        return Response({'error': 'Agent not found'}, status=status.HTTP_404_NOT_FOUND)

    serializer = AgentSteerSerializer(data=request.data)
    serializer.is_valid(raise_exception=True)

    # The run's checkpointer key is its thread id, which is also the steering
    # key — the mailbox and the graph have to agree on what identifies a run.
    log = await (
        ExecutionLog.objects
        .filter(subagent=agent, user=request.user, status='running')
        .order_by('-started_at')
        .afirst()
    )
    if log is None:
        return Response({'error': 'No run is going for this agent.'},
                        status=status.HTTP_404_NOT_FOUND)

    thread_id = (log.input_data or {}).get('thread_id') or ''
    if not thread_id:
        return Response({'error': 'That run cannot be steered.'},
                        status=status.HTTP_409_CONFLICT)

    steering.post(thread_id, serializer.validated_data['message'])
    return Response({'steered': True, 'execution_id': str(log.execution_id),
                     **steering.stats(thread_id)})
