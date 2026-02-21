from rest_framework import serializers
from .models import Workflow, WorkflowVersion, HITLRequest, ConversationMessage
from credentials.models import Credential

class WorkflowSerializer(serializers.ModelSerializer):
    """Serializer for the Workflow model."""
    node_count = serializers.IntegerField(read_only=True)
    llm_credential_id = serializers.PrimaryKeyRelatedField(
        source='llm_credential', 
        queryset=Credential.objects.all(), 
        required=False, 
        allow_null=True
    )

    class Meta:
        model = Workflow
        fields = [
            'id', 'name', 'slug', 'description', 'context', 'nodes', 'edges',
            'viewport', 'workflow_settings', 'supervision_level', 'llm_provider',
            'llm_model', 'llm_credential_id', 'status', 'icon', 'color', 'tags',
            'execution_count', 'last_executed_at', 'created_at', 'updated_at',
            'node_count'
        ]
        read_only_fields = ['id', 'slug', 'created_at', 'updated_at', 'execution_count', 'last_executed_at']

class WorkflowVersionSerializer(serializers.ModelSerializer):
    """Serializer for WorkflowVersion snapshots."""
    class Meta:
        model = WorkflowVersion
        fields = [
            'id', 'version_number', 'label', 'nodes', 'edges', 
            'workflow_settings', 'change_summary', 'created_at'
        ]
        read_only_fields = ['id', 'created_at']

class HITLRequestSerializer(serializers.ModelSerializer):
    """Serializer for Human-in-the-Loop requests."""
    type = serializers.CharField(source='request_type')
    execution_id = serializers.SerializerMethodField()

    class Meta:
        model = HITLRequest
        fields = [
            'request_id', 'type', 'title', 'message', 'options', 
            'node_id', 'execution_id', 'timeout_seconds', 'created_at',
            'status', 'response', 'responded_at'
        ]
        read_only_fields = ['request_id', 'created_at']

    def get_execution_id(self, obj):
        return str(obj.execution.execution_id) if obj.execution else None

class ConversationMessageSerializer(serializers.ModelSerializer):
    """Serializer for AI chat messages."""
    class Meta:
        model = ConversationMessage
        fields = ['role', 'content', 'metadata', 'created_at']
        read_only_fields = ['created_at']
