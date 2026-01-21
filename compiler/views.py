"""
Compiler Views

API endpoints for workflow compilation.
"""
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404

from orchestrator.models import Workflow
from credentials.models import Credential
from core.permissions import IsOwner
from .compiler import WorkflowCompiler


class CompileWorkflowView(APIView):
    """
    Compile a workflow.
    
    POST /api/workflows/{id}/compile/
    
    Validates the workflow DAG, credentials, and node configs.
    Returns compilation result with errors or execution plan.
    """
    permission_classes = [IsAuthenticated]
    throttle_scope = 'compile'
    
    def post(self, request, workflow_id):
        # Get workflow
        workflow = get_object_or_404(
            Workflow,
            id=workflow_id,
            user=request.user
        )
        
        # Get user's credentials
        user_credentials = set(
            str(cred_id) for cred_id in Credential.objects.filter(
                user=request.user,
                is_active=True
            ).values_list('id', flat=True)
        )
        
        # Compile
        workflow_data = {
            'nodes': workflow.nodes,
            'edges': workflow.edges,
            'settings': {
                'workflow_id': workflow.id,
                **workflow.workflow_settings,
            }
        }
        
        compiler = WorkflowCompiler(
            workflow_data,
            request.user,
            user_credentials
        )
        
        result = compiler.compile()
        
        return Response({
            'success': result.success,
            'errors': [e.model_dump() for e in result.errors],
            'warnings': [w.model_dump() for w in result.warnings],
            'execution_plan': result.execution_plan,
            'stats': {
                'node_count': result.node_count,
                'edge_count': result.edge_count,
            }
        }, status=status.HTTP_200_OK if result.success else status.HTTP_400_BAD_REQUEST)


class ValidateWorkflowView(APIView):
    """
    Quick validation without building execution plan.
    
    POST /api/workflows/{id}/validate/
    
    Lighter-weight check for editor validation.
    """
    permission_classes = [IsAuthenticated]
    
    def post(self, request, workflow_id):
        workflow = get_object_or_404(
            Workflow,
            id=workflow_id,
            user=request.user
        )
        
        compiler = WorkflowCompiler(
            {'nodes': workflow.nodes, 'edges': workflow.edges},
            request.user
        )
        
        result = compiler.compile()
        
        return Response({
            'valid': result.success,
            'error_count': len(result.errors),
            'errors': [e.model_dump() for e in result.errors[:5]],  # First 5 errors only
        })
