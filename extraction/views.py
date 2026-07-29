"""Extraction schemas and the rows they produce.

The review queue is the part that carries weight. A row below the schema's
confidence threshold is held rather than accepted, and clearing it is an
explicit act recorded against a user — "who said this ₹48,200 was right?" has to
have an answer, or the table is not usable for accounting.
"""
from django.db.models import Count, Q
from django.utils import timezone
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import ExtractedRow, ExtractionSchema
from .serializers import ExtractedRowSerializer, ExtractionSchemaSerializer


class RowPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class ExtractionSchemaViewSet(viewsets.ModelViewSet):
    serializer_class = ExtractionSchemaSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        return (
            ExtractionSchema.objects
            .filter(user=self.request.user)
            .annotate(
                row_count=Count('rows', distinct=True),
                review_count=Count('rows', filter=Q(rows__status='needs_review'), distinct=True),
            )
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def perform_update(self, serializer):
        schema = serializer.save()
        # Raising or lowering the bar has to re-sort the rows already collected,
        # otherwise the review count shown on the card describes the old rule.
        if 'confidence_threshold' in serializer.validated_data:
            for row in schema.rows.exclude(status__in=('reviewed', 'rejected')):
                row.apply_threshold()
                row.save(update_fields=['status'])

    @action(detail=True, methods=['get', 'post'])
    def rows(self, request, pk=None):
        schema = self.get_object()

        if request.method == 'POST':
            many = isinstance(request.data, list)
            serializer = ExtractedRowSerializer(data=request.data, many=many)
            serializer.is_valid(raise_exception=True)
            rows = serializer.save(schema=schema)
            for row in rows if many else [rows]:
                row.apply_threshold()
                row.save(update_fields=['status'])
            return Response(
                ExtractedRowSerializer(rows, many=many).data, status=status.HTTP_201_CREATED
            )

        qs = schema.rows.all()
        status_filter = request.query_params.get('status')
        if status_filter:
            qs = qs.filter(status=status_filter)

        paginator = RowPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(ExtractedRowSerializer(page, many=True).data)


class ExtractedRowViewSet(viewsets.ModelViewSet):
    serializer_class = ExtractedRowSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = RowPagination

    def get_queryset(self):
        qs = ExtractedRow.objects.filter(schema__user=self.request.user).select_related('schema')
        schema_id = self.request.query_params.get('schema')
        return qs.filter(schema_id=schema_id) if schema_id else qs

    @action(detail=True, methods=['post'])
    def review(self, request, pk=None):
        """Accept a held row, optionally correcting it first.

        A correction is the most valuable kind of training example, so the
        response says whether the values changed — the caller can offer to add
        it to a dataset rather than losing the judgement.
        """
        row = self.get_object()
        corrections = request.data.get('data')
        changed = False

        if corrections is not None:
            if not isinstance(corrections, dict):
                return Response({'error': 'data must be an object of field -> value.'},
                                status=status.HTTP_400_BAD_REQUEST)
            unknown = set(corrections) - {f.get('name') for f in row.schema.fields}
            if unknown:
                return Response(
                    {'error': f"Not fields on this schema: {', '.join(sorted(unknown))}."},
                    status=status.HTTP_400_BAD_REQUEST,
                )
            changed = any(row.data.get(k) != v for k, v in corrections.items())
            row.data = {**row.data, **corrections}

        row.status = 'rejected' if request.data.get('reject') else 'reviewed'
        row.reviewed_by = request.user
        row.reviewed_at = timezone.now()
        row.save(update_fields=['data', 'status', 'reviewed_by', 'reviewed_at'])

        return Response({**ExtractedRowSerializer(row).data, 'corrected': changed})
