"""Eval suites, their cases, and runs.

Scoring a run against a live model is a background job that does not exist yet,
so `POST /suites/{id}/run/` records a queued run and returns it. That is
deliberate rather than a stub: the run row is the thing the UI polls, and having
it exist first means the executor can be dropped in later without the API
changing shape.
"""
from django.db.models import Count
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import EvalCase, EvalRun, EvalSuite
from .serializers import (
    EvalCaseResultSerializer,
    EvalCaseSerializer,
    EvalRunSerializer,
    EvalSuiteSerializer,
)


class EvalSuiteViewSet(viewsets.ModelViewSet):
    serializer_class = EvalSuiteSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return (
            EvalSuite.objects
            .filter(user=self.request.user)
            # Aliased away from `case_count`: the model exposes that as a
            # property, and an annotation cannot be assigned over one.
            .annotate(cases_total=Count('cases', distinct=True))
            .select_related('agent', 'dataset')
            .order_by('-updated_at')
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get', 'post'])
    def cases(self, request, pk=None):
        suite = self.get_object()
        if request.method == 'POST':
            serializer = EvalCaseSerializer(data=request.data, many=isinstance(request.data, list))
            serializer.is_valid(raise_exception=True)
            serializer.save(suite=suite)
            return Response(serializer.data, status=status.HTTP_201_CREATED)
        return Response(EvalCaseSerializer(suite.cases.all(), many=True).data)

    @action(detail=True, methods=['get'])
    def runs(self, request, pk=None):
        suite = self.get_object()
        return Response(EvalRunSerializer(suite.runs.all()[:50], many=True).data)

    @action(detail=True, methods=['post'])
    def run(self, request, pk=None):
        """Queue a run of this suite."""
        suite = self.get_object()
        total = suite.cases.count()
        if not total:
            return Response(
                {'error': 'This suite has no cases, so there is nothing to score.'},
                status=status.HTTP_400_BAD_REQUEST,
            )

        run = EvalRun.objects.create(
            suite=suite,
            user=request.user,
            provider=request.data.get('provider') or (suite.agent.llm_provider if suite.agent else ''),
            model=request.data.get('model') or (suite.agent.llm_model if suite.agent else ''),
            status='queued',
            total_cases=total,
        )
        return Response(EvalRunSerializer(run).data, status=status.HTTP_201_CREATED)


class EvalCaseViewSet(viewsets.ModelViewSet):
    serializer_class = EvalCaseSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return EvalCase.objects.filter(suite__user=self.request.user)


class EvalRunViewSet(viewsets.ReadOnlyModelViewSet):
    """Runs are records of something that happened; they are not edited."""
    serializer_class = EvalRunSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = EvalRun.objects.filter(user=self.request.user).select_related('suite')
        suite_id = self.request.query_params.get('suite')
        return qs.filter(suite_id=suite_id) if suite_id else qs

    @action(detail=True, methods=['get'])
    def results(self, request, pk=None):
        run = self.get_object()
        qs = run.results.select_related('case')
        only = request.query_params.get('only')
        if only == 'failed':
            qs = qs.filter(passed=False)
        elif only == 'passed':
            qs = qs.filter(passed=True)
        return Response(EvalCaseResultSerializer(qs, many=True).data)

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        run = self.get_object()
        if run.status not in ('queued', 'running'):
            return Response(
                {'error': f'This run already {run.status}; there is nothing to cancel.'},
                status=status.HTTP_409_CONFLICT,
            )
        run.status = 'cancelled'
        run.completed_at = timezone.now()
        run.save(update_fields=['status', 'completed_at'])
        return Response(EvalRunSerializer(run).data)
