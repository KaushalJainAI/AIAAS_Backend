from rest_framework import serializers
from .models import ExecutionLog, NodeExecutionLog, AuditEntry, OrchestratorThought

class OrchestratorThoughtSerializer(serializers.ModelSerializer):
    """Serializer for orchestrator thoughts."""
    class Meta:
        model = OrchestratorThought
        fields = [
            'id', 'execution', 'node_id', 'node_name', 'category', 
            'thought_type', 'content', 'reasoning', 
            'model_id', 'model_name',
            'metadata', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

class AnalyticsFilterSerializer(serializers.Serializer):
    """Serializer for basic analytics filtering."""
    days = serializers.IntegerField(default=30, min_value=1, max_value=365)
    workflow_id = serializers.IntegerField(required=False, allow_null=True)

class AuditFilterSerializer(serializers.Serializer):
    """Serializer for audit log filtering and pagination."""
    action_type = serializers.CharField(required=False, allow_null=True)
    workflow_id = serializers.IntegerField(required=False, allow_null=True)
    limit = serializers.IntegerField(default=50, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)

class ExecutionListFilterSerializer(serializers.Serializer):
    """Serializer for execution log listing and pagination."""
    workflow_id = serializers.IntegerField(required=False, allow_null=True)
    status = serializers.CharField(required=False, allow_null=True)
    limit = serializers.IntegerField(default=20, min_value=1, max_value=100)
    offset = serializers.IntegerField(default=0, min_value=0)

class AuditExportSerializer(serializers.Serializer):
    """Serializer for audit export parameters."""
    format = serializers.ChoiceField(choices=['json', 'csv'], default='json')
    days = serializers.IntegerField(default=30, min_value=1, max_value=365)
