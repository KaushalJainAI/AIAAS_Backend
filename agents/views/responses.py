"""
Inline response serializers shared by the orchestrator view modules.

They exist for the OpenAPI schema, not for parsing, which is why they are here
rather than in `orchestrator.serializers` alongside the model serializers.
"""

from drf_spectacular.utils import inline_serializer
from rest_framework import serializers as drf_serializers


_ExecutionStartedResponse = inline_serializer(
    name="ExecutionStartedResponse",
    fields={
        "execution_id": drf_serializers.CharField(),
        "workflow_id": drf_serializers.IntegerField(),
        "state": drf_serializers.CharField(),
        "started_at": drf_serializers.DateTimeField(allow_null=True),
    },
)

_ErrorResponse = inline_serializer(
    name="OrchestratorErrorResponse",
    fields={
        "error": drf_serializers.CharField(),
        "message": drf_serializers.CharField(required=False),
    },
)
