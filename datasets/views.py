"""Dataset CRUD, plus paged access to rows.

Rows are a sub-resource rather than a nested field: a gold set runs to thousands
of rows, and serializing them all into the list response would make the datasets
page slow in proportion to how much work you have done — exactly backwards.
"""
from django.db.models import Count
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.pagination import PageNumberPagination
from rest_framework.response import Response

from .models import Dataset, DatasetRow
from .serializers import DatasetRowSerializer, DatasetSerializer


class RowPagination(PageNumberPagination):
    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 500


class DatasetViewSet(viewsets.ModelViewSet):
    serializer_class = DatasetSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Aliased away from `row_count`: the model already exposes that as a
        # property, and Django cannot assign an annotation over one.
        return (
            Dataset.objects
            .filter(user=self.request.user)
            .annotate(rows_total=Count('rows', distinct=True))
            .prefetch_related('eval_suites', 'tuning_jobs')
            .order_by('-updated_at')
        )

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    @action(detail=True, methods=['get', 'post'])
    def rows(self, request, pk=None):
        dataset = self.get_object()

        if request.method == 'POST':
            serializer = DatasetRowSerializer(data=request.data, many=isinstance(request.data, list))
            serializer.is_valid(raise_exception=True)
            serializer.save(dataset=dataset)
            return Response(serializer.data, status=status.HTTP_201_CREATED)

        qs = dataset.rows.all()
        split = request.query_params.get('split')
        if split:
            qs = qs.filter(split=split)

        paginator = RowPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        return paginator.get_paginated_response(DatasetRowSerializer(page, many=True).data)

    @action(detail=True, methods=['get'])
    def stats(self, request, pk=None):
        """Row counts per split — what the detail page header shows."""
        dataset = self.get_object()
        counts = dict(
            dataset.rows.values_list('split').annotate(n=Count('id')).values_list('split', 'n')
        )
        return Response({
            'total': sum(counts.values()),
            'train': counts.get('train', 0),
            'val': counts.get('val', 0),
            'test': counts.get('test', 0),
        })


class DatasetRowViewSet(viewsets.ModelViewSet):
    """Editing a single row — the correction path."""
    serializer_class = DatasetRowSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = RowPagination

    def get_queryset(self):
        return DatasetRow.objects.filter(dataset__user=self.request.user)
