import re

from rest_framework import serializers

from .models import ConversationMessage, HITLRequest


#: Shared guard for agent names. The name reaches logs, filenames and the
#: admin, so the check is about what a name may contain.
SUSPICIOUS_WORKFLOW_NAME_RE = re.compile(
    r"(\.\./|[;|`<>]|--|/\*|\*/|\$\(|\b(drop|union|select|insert|delete|update|exec|whoami|passwd)\b)",
    re.IGNORECASE,
)

class HITLRequestSerializer(serializers.ModelSerializer):
    """Serializer for Human-in-the-Loop requests."""
    # The frontend (Inbox, Overview) reads `request_type` and `workflow_name`.
    # `type` is kept as an alias so nothing that already read it breaks.
    type = serializers.CharField(source='request_type', read_only=True)
    execution_id = serializers.SerializerMethodField()
    workflow_name = serializers.SerializerMethodField()
    detail = serializers.SerializerMethodField()

    class Meta:
        model = HITLRequest
        fields = [
            'request_id', 'request_type', 'type', 'title', 'message', 'options',
            'detail', 'node_id', 'execution_id', 'workflow_name',
            'timeout_seconds', 'created_at', 'status', 'response', 'responded_at'
        ]
        read_only_fields = ['request_id', 'created_at']

    def get_detail(self, obj):
        """What the agent is asking to do, as named fields.

        Lifted out of `context_data` rather than exposing that column, which
        also carries the thread id and the agent id — routing the client needs
        no part of. Absent on every row written before `describe_call` existed,
        so the Inbox falls back to `message`.
        """
        detail = (obj.context_data or {}).get('detail')
        return detail if isinstance(detail, dict) and detail else None

    def get_execution_id(self, obj):
        return str(obj.execution.execution_id) if obj.execution else None

    def get_workflow_name(self, obj):
        # The field keeps its API name because the frontend Inbox and Overview
        # render it; what it *means* is "which agent is blocked".
        execution = getattr(obj, 'execution', None)
        agent = getattr(execution, 'subagent', None) if execution else None
        return agent.name if agent else None

class ConversationMessageSerializer(serializers.ModelSerializer):
    """Serializer for AI chat messages."""
    class Meta:
        model = ConversationMessage
        fields = ['id', 'role', 'content', 'metadata', 'created_at']
        read_only_fields = ['id', 'created_at']
