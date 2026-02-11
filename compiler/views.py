"""
Compiler Views

API endpoints for workflow compilation.
"""
from adrf.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework import status
from django.shortcuts import get_object_or_404
from asgiref.sync import sync_to_async

from orchestrator.models import Workflow
from credentials.models import Credential
from .compiler import WorkflowCompiler, WorkflowCompilationError


class CompileWorkflowView(APIView):
    """
    Compile a workflow.
    """
    permission_classes = [IsAuthenticated]
    throttle_scope = 'compile'
    
    async def post(self, request, workflow_id):
        # Get workflow
        workflow = await sync_to_async(get_object_or_404)(
            Workflow,
            id=workflow_id,
            user=request.user
        )
        
        # Get user's credentials
        def get_cred_ids():
            return set(
                str(cred_id) for cred_id in Credential.objects.filter(
                    user=request.user,
                    is_active=True
                ).values_list('id', flat=True)
            )
            
        user_credentials = await sync_to_async(get_cred_ids)()
        
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
        
        try:
            # compile() is sync, wrap it
            await sync_to_async(compiler.compile)()
            
            return Response({
                'success': True,
                'errors': [],
                'warnings': [], 
                'execution_plan': {},
                'stats': {
                    'node_count': len(workflow.nodes),
                    'edge_count': len(workflow.edges),
                }
            }, status=status.HTTP_200_OK)
            
        except WorkflowCompilationError as e:
            serialized_errors = []
            for err in e.errors:
                if hasattr(err, 'model_dump'):
                    serialized_errors.append(err.model_dump(by_alias=True))
                elif isinstance(err, dict):
                    serialized_errors.append(err)
                else:
                    serialized_errors.append({'message': str(err), 'code': 'COMPILATION_ERROR', 'type': 'error'})

            return Response({
                'success': False,
                'errors': serialized_errors,
                'warnings': [],
                'execution_plan': {},
                'stats': {}
            }, status=status.HTTP_400_BAD_REQUEST)


class ValidateWorkflowView(APIView):
    """
    Quick validation without building execution plan.
    """
    permission_classes = [IsAuthenticated]
    
    async def post(self, request, workflow_id):
        workflow = await sync_to_async(get_object_or_404)(
            Workflow,
            id=workflow_id,
            user=request.user
        )
        
        compiler = WorkflowCompiler(
            {'nodes': workflow.nodes, 'edges': workflow.edges},
            request.user
        )
        
        try:
            await sync_to_async(compiler.compile)()
            return Response({
                'valid': True,
                'error_count': 0,
                'errors': [],
            })
        except WorkflowCompilationError as e:
            serialized_errors = []
            for err in e.errors:
                if hasattr(err, 'model_dump'):
                    serialized_errors.append(err.model_dump(by_alias=True))
                elif isinstance(err, dict):
                    serialized_errors.append(err)
                else:
                    serialized_errors.append({'message': str(err), 'code': 'VALIDATION_ERROR', 'type': 'error'})

            return Response({
                'valid': False,
                'error_count': len(e.errors),
                'errors': serialized_errors[:5],
            })


class AdHocValidateWorkflowView(APIView):
    """
    Validate a workflow definition without saving it.
    """
    permission_classes = [IsAuthenticated]
    
    async def post(self, request):
        data = request.data
        nodes = data.get('nodes', [])
        edges = data.get('edges', [])
        
        # Determine credentials
        def get_cred_ids():
            return set(
                str(cred_id) for cred_id in Credential.objects.filter(
                    user=request.user,
                    is_active=True
                ).values_list('id', flat=True)
            )
            
        user_credentials = await sync_to_async(get_cred_ids)()
        
        # Prepare compiler
        workflow_data = {
            'nodes': nodes,
            'edges': edges,
            'settings': {}
        }
        
        compiler = WorkflowCompiler(
            workflow_data,
            request.user,
            user_credentials
        )
        
        try:
            await sync_to_async(compiler.compile)()
            return Response({
                'is_valid': True,
                'errors': [],
                'warnings': [],
                'info': [], 
            })
        except WorkflowCompilationError as e:
            serialized_errors = []
            for err in e.errors:
                if hasattr(err, 'model_dump'):
                    serialized_errors.append(err.model_dump(by_alias=True))
                elif isinstance(err, dict):
                    serialized_errors.append(err)
                else:
                    serialized_errors.append({'message': str(err), 'code': 'VALIDATION_ERROR', 'type': 'error'})

            return Response({
                'is_valid': False,
                'errors': serialized_errors,
                'warnings': [],
                'info': [],
            })
