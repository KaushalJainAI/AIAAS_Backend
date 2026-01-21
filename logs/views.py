"""
Logs App API Views - Insights and Analytics Endpoints

Provides execution statistics, per-workflow metrics, and audit trail APIs.
"""
from datetime import timedelta
from typing import Any

from django.db.models import Count, Sum, Avg, F, Q
from django.db.models.functions import TruncDate, TruncHour
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from .models import ExecutionLog, NodeExecutionLog, AuditEntry


# ======================== Insights/Analytics API ========================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_statistics(request):
    """
    Get execution statistics for the authenticated user.
    
    Query params:
        - days: Number of days to look back (default: 30)
        - workflow_id: Filter by specific workflow (optional)
    
    Response:
        {
            "summary": {
                "total_executions": int,
                "successful": int,
                "failed": int,
                "success_rate": float,
                "avg_duration_ms": float,
                "total_nodes_executed": int,
                "total_tokens_used": int
            },
            "by_status": {"completed": int, "failed": int, ...},
            "by_trigger": {"manual": int, "webhook": int, ...},
            "daily_trend": [{"date": "2024-01-21", "count": int, "success": int}, ...]
        }
    """
    days = int(request.query_params.get('days', 30))
    workflow_id = request.query_params.get('workflow_id')
    
    start_date = timezone.now() - timedelta(days=days)
    
    # Base queryset
    qs = ExecutionLog.objects.filter(
        user=request.user,
        created_at__gte=start_date
    )
    
    if workflow_id:
        qs = qs.filter(workflow_id=workflow_id)
    
    # Summary statistics
    total = qs.count()
    completed = qs.filter(status='completed').count()
    failed = qs.filter(status='failed').count()
    
    aggregates = qs.aggregate(
        avg_duration=Avg('duration_ms'),
        total_nodes=Sum('nodes_executed'),
        total_tokens=Sum('tokens_used'),
    )
    
    summary = {
        "total_executions": total,
        "successful": completed,
        "failed": failed,
        "success_rate": round(completed / total * 100, 1) if total > 0 else 0,
        "avg_duration_ms": round(aggregates['avg_duration'] or 0, 0),
        "total_nodes_executed": aggregates['total_nodes'] or 0,
        "total_tokens_used": aggregates['total_tokens'] or 0,
    }
    
    # Group by status
    by_status = dict(
        qs.values('status')
        .annotate(count=Count('id'))
        .values_list('status', 'count')
    )
    
    # Group by trigger type
    by_trigger = dict(
        qs.values('trigger_type')
        .annotate(count=Count('id'))
        .values_list('trigger_type', 'count')
    )
    
    # Daily trend
    daily_trend = list(
        qs.annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(
            count=Count('id'),
            success=Count('id', filter=Q(status='completed'))
        )
        .order_by('date')
        .values('date', 'count', 'success')
    )
    
    # Convert dates to strings
    for item in daily_trend:
        item['date'] = item['date'].isoformat() if item['date'] else None
    
    return Response({
        "summary": summary,
        "by_status": by_status,
        "by_trigger": by_trigger,
        "daily_trend": daily_trend,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def workflow_metrics(request, workflow_id: int):
    """
    Get detailed metrics for a specific workflow.
    
    Response:
        {
            "workflow_id": int,
            "total_executions": int,
            "avg_duration_ms": float,
            "success_rate": float,
            "recent_executions": [...],
            "node_success_rates": {...},
            "error_hotspots": [...]
        }
    """
    # Verify workflow belongs to user
    from orchestrator.models import Workflow
    try:
        workflow = Workflow.objects.get(id=workflow_id, user=request.user)
    except Workflow.DoesNotExist:
        return Response({"error": "Workflow not found"}, status=404)
    
    executions = ExecutionLog.objects.filter(workflow=workflow)
    
    # Basic stats
    total = executions.count()
    completed = executions.filter(status='completed').count()
    
    aggregates = executions.aggregate(
        avg_duration=Avg('duration_ms'),
        total_tokens=Sum('tokens_used'),
    )
    
    # Recent executions
    recent = list(
        executions.order_by('-created_at')[:10]
        .values('execution_id', 'status', 'duration_ms', 'created_at', 'trigger_type')
    )
    
    # Node success rates
    node_logs = NodeExecutionLog.objects.filter(execution__workflow=workflow)
    node_stats = (
        node_logs.values('node_id', 'node_name')
        .annotate(
            total=Count('id'),
            success=Count('id', filter=Q(status='completed')),
        )
    )
    node_success_rates = {
        n['node_id']: {
            "name": n['node_name'],
            "success_rate": round(n['success'] / n['total'] * 100, 1) if n['total'] > 0 else 0,
            "total_runs": n['total'],
        }
        for n in node_stats
    }
    
    # Error hotspots (nodes that fail most)
    error_hotspots = list(
        node_logs
        .filter(status='failed')
        .values('node_id', 'node_name', 'node_type')
        .annotate(error_count=Count('id'))
        .order_by('-error_count')[:5]
    )
    
    return Response({
        "workflow_id": workflow_id,
        "workflow_name": workflow.name,
        "total_executions": total,
        "avg_duration_ms": round(aggregates['avg_duration'] or 0, 0),
        "success_rate": round(completed / total * 100, 1) if total > 0 else 0,
        "total_tokens_used": aggregates['total_tokens'] or 0,
        "recent_executions": recent,
        "node_success_rates": node_success_rates,
        "error_hotspots": error_hotspots,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def cost_breakdown(request):
    """
    Get token/credit usage breakdown.
    
    Query params:
        - days: Number of days to look back (default: 30)
    
    Response:
        {
            "period_days": int,
            "total_tokens": int,
            "total_credits": int,
            "by_workflow": [...],
            "by_node_type": [...],
            "daily_usage": [...]
        }
    """
    days = int(request.query_params.get('days', 30))
    start_date = timezone.now() - timedelta(days=days)
    
    executions = ExecutionLog.objects.filter(
        user=request.user,
        created_at__gte=start_date
    )
    
    # Total usage
    totals = executions.aggregate(
        total_tokens=Sum('tokens_used'),
        total_credits=Sum('credits_used'),
    )
    
    # By workflow
    by_workflow = list(
        executions
        .values('workflow__id', 'workflow__name')
        .annotate(
            tokens=Sum('tokens_used'),
            credits=Sum('credits_used'),
            executions=Count('id'),
        )
        .order_by('-tokens')[:10]
    )
    
    # By node type
    node_logs = NodeExecutionLog.objects.filter(
        execution__user=request.user,
        execution__created_at__gte=start_date,
    )
    
    by_node_type = list(
        node_logs
        .values('node_type')
        .annotate(
            count=Count('id'),
            # Extract tokens from output_data if available
        )
        .order_by('-count')
    )
    
    # Daily usage trend
    daily_usage = list(
        executions
        .annotate(date=TruncDate('created_at'))
        .values('date')
        .annotate(
            tokens=Sum('tokens_used'),
            credits=Sum('credits_used'),
        )
        .order_by('date')
    )
    
    for item in daily_usage:
        item['date'] = item['date'].isoformat() if item['date'] else None
    
    return Response({
        "period_days": days,
        "total_tokens": totals['total_tokens'] or 0,
        "total_credits": totals['total_credits'] or 0,
        "by_workflow": by_workflow,
        "by_node_type": by_node_type,
        "daily_usage": daily_usage,
    })


# ======================== Audit Trail API ========================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def audit_list(request):
    """
    List audit entries for the user.
    
    Query params:
        - action_type: Filter by action type (optional)
        - workflow_id: Filter by workflow (optional)
        - limit: Number of entries (default: 50)
        - offset: Pagination offset (default: 0)
    
    Response:
        {
            "count": int,
            "results": [...]
        }
    """
    action_type = request.query_params.get('action_type')
    workflow_id = request.query_params.get('workflow_id')
    limit = min(int(request.query_params.get('limit', 50)), 100)
    offset = int(request.query_params.get('offset', 0))
    
    qs = AuditEntry.objects.filter(user=request.user)
    
    if action_type:
        qs = qs.filter(action_type=action_type)
    
    if workflow_id:
        qs = qs.filter(workflow_id=workflow_id)
    
    total = qs.count()
    
    entries = list(
        qs.order_by('-created_at')[offset:offset + limit]
        .values(
            'id', 'action_type', 'request_details', 'response',
            'workflow_id', 'node_id', 'created_at', 'ip_address'
        )
    )
    
    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": entries,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def audit_export(request):
    """
    Export audit entries as JSON.
    
    Query params:
        - format: 'json' or 'csv' (default: json)
        - days: Number of days to export (default: 30)
    """
    from django.http import HttpResponse
    import csv
    import json as json_lib
    from io import StringIO
    
    export_format = request.query_params.get('format', 'json')
    days = int(request.query_params.get('days', 30))
    
    start_date = timezone.now() - timedelta(days=days)
    
    entries = AuditEntry.objects.filter(
        user=request.user,
        created_at__gte=start_date
    ).order_by('-created_at')
    
    if export_format == 'csv':
        output = StringIO()
        writer = csv.writer(output)
        writer.writerow(['Timestamp', 'Action Type', 'Workflow ID', 'Node ID', 'IP Address', 'Details'])
        
        for entry in entries:
            writer.writerow([
                entry.created_at.isoformat(),
                entry.action_type,
                entry.workflow_id or '',
                entry.node_id,
                entry.ip_address or '',
                json_lib.dumps(entry.request_details),
            ])
        
        response = HttpResponse(output.getvalue(), content_type='text/csv')
        response['Content-Disposition'] = f'attachment; filename="audit_export_{timezone.now().date()}.csv"'
        return response
    
    else:  # JSON
        data = list(
            entries.values(
                'id', 'action_type', 'request_details', 'response',
                'workflow_id', 'node_id', 'created_at', 'ip_address'
            )
        )
        
        response = HttpResponse(
            json_lib.dumps(data, default=str, indent=2),
            content_type='application/json'
        )
        response['Content-Disposition'] = f'attachment; filename="audit_export_{timezone.now().date()}.json"'
        return response


# ======================== Execution History API ========================

@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_list(request):
    """
    List executions for the user.
    
    Query params:
        - workflow_id: Filter by workflow (optional)
        - status: Filter by status (optional)
        - limit: Number of entries (default: 20)
        - offset: Pagination offset (default: 0)
    """
    workflow_id = request.query_params.get('workflow_id')
    exec_status = request.query_params.get('status')
    limit = min(int(request.query_params.get('limit', 20)), 100)
    offset = int(request.query_params.get('offset', 0))
    
    qs = ExecutionLog.objects.filter(user=request.user)
    
    if workflow_id:
        qs = qs.filter(workflow_id=workflow_id)
    
    if exec_status:
        qs = qs.filter(status=exec_status)
    
    total = qs.count()
    
    executions = list(
        qs.order_by('-created_at')[offset:offset + limit]
        .values(
            'execution_id', 'workflow_id', 'workflow__name',
            'status', 'trigger_type', 'duration_ms',
            'nodes_executed', 'tokens_used', 'error_message',
            'started_at', 'completed_at', 'created_at'
        )
    )
    
    return Response({
        "count": total,
        "limit": limit,
        "offset": offset,
        "results": executions,
    })


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def execution_detail(request, execution_id: str):
    """
    Get detailed execution information including node logs.
    """
    try:
        execution = ExecutionLog.objects.get(
            execution_id=execution_id,
            user=request.user
        )
    except ExecutionLog.DoesNotExist:
        return Response({"error": "Execution not found"}, status=404)
    
    # Get node logs
    node_logs = list(
        execution.node_logs.order_by('execution_order')
        .values(
            'node_id', 'node_type', 'node_name', 'status',
            'execution_order', 'duration_ms', 'error_message',
            'input_data', 'output_data', 'started_at', 'completed_at'
        )
    )
    
    return Response({
        "execution_id": str(execution.execution_id),
        "workflow_id": execution.workflow_id,
        "workflow_name": execution.workflow.name if execution.workflow else None,
        "status": execution.status,
        "trigger_type": execution.trigger_type,
        "duration_ms": execution.duration_ms,
        "nodes_executed": execution.nodes_executed,
        "tokens_used": execution.tokens_used,
        "credits_used": execution.credits_used,
        "input_data": execution.input_data,
        "output_data": execution.output_data,
        "error_message": execution.error_message,
        "error_node_id": execution.error_node_id,
        "started_at": execution.started_at,
        "completed_at": execution.completed_at,
        "node_logs": node_logs,
    })
