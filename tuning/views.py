"""Fine-tuning jobs.

Creating a job records the intent and queues it; the trainer itself is a
background worker that does not exist yet. The row is what the UI polls, so it
exists from the moment you ask for it rather than appearing once training starts.
"""
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from .models import TuningJob
from .serializers import TuningJobSerializer


class TuningJobViewSet(viewsets.ModelViewSet):
    serializer_class = TuningJobSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        qs = TuningJob.objects.filter(user=self.request.user).select_related('dataset')
        status_filter = self.request.query_params.get('status')
        return qs.filter(status=status_filter) if status_filter else qs

    def perform_create(self, serializer):
        serializer.save(user=self.request.user, status='queued')

    def perform_destroy(self, instance):
        # A deployed model may be serving traffic; deleting the job would strip
        # the only record of what it was trained on.
        if instance.status == 'deployed':
            from rest_framework.exceptions import ValidationError
            raise ValidationError(
                'This job is deployed. Undeploy it before deleting, so nothing '
                'is left serving from a model with no provenance.'
            )
        instance.delete()

    @action(detail=True, methods=['post'])
    def cancel(self, request, pk=None):
        job = self.get_object()
        if job.status not in ('queued', 'training'):
            return Response(
                {'error': f'This job is {job.status}; there is nothing to cancel.'},
                status=status.HTTP_409_CONFLICT,
            )
        job.status = 'cancelled'
        job.completed_at = timezone.now()
        job.save(update_fields=['status', 'completed_at'])
        return Response(TuningJobSerializer(job, context={'request': request}).data)

    @action(detail=True, methods=['post'])
    def deploy(self, request, pk=None):
        job = self.get_object()
        if job.status != 'completed':
            return Response(
                {'error': 'Only a completed job can be deployed.'},
                status=status.HTTP_409_CONFLICT,
            )
        job.status = 'deployed'
        job.save(update_fields=['status'])
        return Response(TuningJobSerializer(job, context={'request': request}).data)
