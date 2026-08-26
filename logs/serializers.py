"""
Query-parameter validation for `/api/logs/`.

These are `Serializer`s used as input validators, not output renderers — the
responses are assembled as plain dicts in `queries.py`, because most of them are
aggregates with no single model behind them.

`workflow_id` keeps its name because it is part of the wire contract; see the
module docstring in `views.py`.
"""
from rest_framework import serializers


class AnalyticsFilterSerializer(serializers.Serializer):
    """Filters for the insights endpoints."""

    days = serializers.IntegerField(default=30, min_value=1, max_value=365)
    workflow_id = serializers.IntegerField(required=False, allow_null=True)


class ExecutionListFilterSerializer(serializers.Serializer):
    """Filters and cursor for the execution list.

    Pagination is cursor-based, so there is deliberately no `offset` here: an
    `offset` alongside a cursor is a promise the API cannot keep (the query
    layer reads the cursor and ignores the number). A caller that needs a total
    gets it from the first (uncursored) page's `count`.
    """

    workflow_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_null=True)
    #: Who started the run. Validated against the model's own choices so a
    #: typo returns 400 rather than an empty list that reads as "no such runs".
    caller = serializers.ChoiceField(
        choices=['api', 'chat', 'orchestrator', 'trigger'],
        required=False, allow_null=True,
    )
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    cursor = serializers.CharField(required=False, allow_null=True, allow_blank=True)
