"""
Compiler API endpoints.

Three views share the same compile-and-respond shape; the _compile_response
helper consolidates error serialisation and outcome formatting so each view
only differs in how it fetches the workflow definition.
"""
from __future__ import annotations

from typing import Any

from adrf.views import APIView
from asgiref.sync import sync_to_async
from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from credentials.models import Credential
from orchestrator.models import Workflow

from .compiler import WorkflowCompiler, WorkflowCompilationError
from .serializers import CompilationResultSerializer, WorkflowDefinitionSerializer


async def _get_user_credential_ids(user) -> set[str]:
    """Return the set of active credential IDs (as strings) owned by the user."""
    def _fetch() -> set[str]:
        return {
            str(cid)
            for cid in Credential.objects.filter(user=user, is_active=True).values_list(
                "id", flat=True,
            )
        }
    return await sync_to_async(_fetch)()


def _serialize_errors(errors: list[Any]) -> list[dict]:
    """Turn a mixed list of CompileError / dict / arbitrary objects into dicts."""
    out: list[dict] = []
    for err in errors:
        if hasattr(err, "model_dump"):
            out.append(err.model_dump(by_alias=True))
        elif isinstance(err, dict):
            out.append(err)
        else:
            out.append({"message": str(err), "code": "COMPILATION_ERROR", "type": "error"})
    return out


async def _compile(
    nodes: list[dict], edges: list[dict], settings: dict, user,
) -> tuple[bool, list[dict]]:
    """Run the compiler, return (success, serialized_errors)."""
    user_credentials = await _get_user_credential_ids(user)
    compiler = WorkflowCompiler(
        {"nodes": nodes, "edges": edges, "settings": settings},
        user,
        user_credentials,
    )
    try:
        await sync_to_async(compiler.compile)()
        return True, []
    except WorkflowCompilationError as e:
        return False, _serialize_errors(e.errors)


class CompileWorkflowView(APIView):
    """Compile a saved workflow; returns 200 on success, 400 on validation failure."""
    permission_classes = [IsAuthenticated]
    throttle_scope = "compile"

    async def post(self, request, workflow_id):
        workflow = await sync_to_async(get_object_or_404)(
            Workflow, id=workflow_id, user=request.user,
        )

        settings = {"workflow_id": workflow.id, **(workflow.workflow_settings or {})}
        ok, errors = await _compile(workflow.nodes, workflow.edges, settings, request.user)

        result = {
            "success": ok,
            "errors": errors,
            "warnings": [],
            "execution_plan": {},
            "stats": {
                "node_count": len(workflow.nodes) if ok else 0,
                "edge_count": len(workflow.edges) if ok else 0,
            },
        }
        http_status = status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST
        return Response(CompilationResultSerializer(result).data, status=http_status)


class ValidateWorkflowView(APIView):
    """
    Quick validation of a saved workflow. Always returns 200; the `valid`
    field distinguishes outcome. Mirrors CompileWorkflowView's full compile
    semantics (including credentials), so the two never disagree.
    """
    permission_classes = [IsAuthenticated]

    async def post(self, request, workflow_id):
        workflow = await sync_to_async(get_object_or_404)(
            Workflow, id=workflow_id, user=request.user,
        )
        ok, errors = await _compile(
            workflow.nodes, workflow.edges, workflow.workflow_settings or {}, request.user,
        )
        return Response({
            "valid": ok,
            "error_count": len(errors),
            "errors": errors[:5] if not ok else [],
        })


class AdHocValidateWorkflowView(APIView):
    """Validate an in-flight workflow definition without saving it."""
    permission_classes = [IsAuthenticated]

    async def post(self, request):
        serializer = WorkflowDefinitionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        nodes = serializer.validated_data.get("nodes", [])
        edges = serializer.validated_data.get("edges", [])
        settings = serializer.validated_data.get("settings", {})

        ok, errors = await _compile(nodes, edges, settings, request.user)
        return Response({
            "is_valid": ok,
            "errors": errors,
            "warnings": [],
            "info": [],
        })
