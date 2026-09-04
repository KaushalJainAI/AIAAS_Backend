from rest_framework import serializers
from .models import ChatSession, ChatMessage, ChatAttachment


class ChatAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatAttachment
        fields = ['id', 'filename', 'file_type', 'file_size', 'created_at']
        read_only_fields = ['id', 'created_at']


class ChatMessageSerializer(serializers.ModelSerializer):
    attachments = ChatAttachmentSerializer(many=True, read_only=True)
    #: A decimal string, never a float. JSON has no decimal type, and a cost
    #: that picks up binary drift on the way to the browser will not add up
    #: against the one the server recorded.
    cost_usd = serializers.DecimalField(
        max_digits=12, decimal_places=6, read_only=True, coerce_to_string=True,
    )

    class Meta:
        model = ChatMessage
        fields = [
            'id', 'role', 'content', 'message_type', 'metadata', 'attachments',
            'created_at',
            # What this one answer cost. Present on assistant rows; a user
            # message is all zeroes with an empty `cost_source`, which reads as
            # `unpriced` on the client and renders as nothing at all.
            'model_id', 'input_tokens', 'output_tokens', 'cached_read_tokens',
            'cached_write_tokens', 'cost_usd', 'cost_source',
        ]
        read_only_fields = fields


class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    title = serializers.CharField(required=True, allow_blank=False, trim_whitespace=True)
    total_cost_usd = serializers.DecimalField(
        max_digits=12, decimal_places=6, read_only=True, coerce_to_string=True,
    )

    class Meta:
        model = ChatSession
        fields = [
            'id', 'title', 'intent', 'llm_provider', 'llm_model', 'llm_effort',
            'system_prompt', 'memory_enabled', 'total_tokens_used',
            'total_cost_usd', 'cost_source',
            'created_at', 'updated_at', 'messages'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'messages', 'total_tokens_used',
            'total_cost_usd', 'cost_source',
        ]

    def validate_llm_effort(self, value):
        """Reject a level that is not on the ladder.

        Blank is valid and means the model's own default — the value every
        session starts at, and the only way back off the knob. Validated
        against the ladder rather than against this session's model, because
        the model can be changed in the same PATCH and `llm.access` snaps a
        level the model does not serve at call time anyway.
        """
        from llm.effort import normalize

        text = (value or '').strip()
        if not text:
            return ''
        level = normalize(text)
        if level is None:
            raise serializers.ValidationError(
                'Not a reasoning effort level.'
            )
        return level

    def validate_title(self, value):
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title is required and cannot be blank.")
        return title
