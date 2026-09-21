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
    feedback = serializers.SerializerMethodField(read_only=True)

    class Meta:
        model = ChatMessage
        fields = [
            'id', 'role', 'content', 'message_type', 'metadata', 'attachments',
            'created_at',
            # What this one answer cost. Present on assistant rows; a user
            # message is all zeroes with an empty `cost_source`, which reads as
            # `unpriced` on the client and renders as nothing at all.
            'model_id', 'input_tokens', 'output_tokens', 'cached_read_tokens',
            'cached_write_tokens', 'cost_usd', 'cost_source', 'paid_by',
            'feedback',
        ]
        read_only_fields = fields

    def get_feedback(self, obj):
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if user is None or getattr(user, 'is_anonymous', False):
            return None
        # Batched by `ChatSessionSerializer` for a whole transcript: one query
        # for every message's feedback, not one per message.
        batch = self.context.get('feedback_by_message')
        if batch is not None:
            return batch.get(obj.pk)
        try:
            from logs.queries import feedback_for

            return feedback_for(user, message_id=obj.pk)
        except Exception:  # noqa: BLE001
            return None


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
            'system_prompt', 'memory_enabled', 'autonomy', 'total_tokens_used',
            'total_cost_usd', 'cost_source', 'paid_by',
            'created_at', 'updated_at', 'messages'
        ]
        read_only_fields = [
            'id', 'created_at', 'updated_at', 'messages', 'total_tokens_used',
            'total_cost_usd', 'cost_source', 'paid_by',
        ]

    def to_representation(self, instance):
        # Every message's feedback in one query, handed to the message
        # serializer through the shared context. Per-message lookups made the
        # detail cost grow with the transcript
        # (`test_session_list::test_the_detail_does_not_query_per_message`).
        request = self.context.get('request')
        user = getattr(request, 'user', None)
        if 'messages' in self.fields and user is not None and not getattr(user, 'is_anonymous', True):
            try:
                from logs.models import Feedback

                self.context['feedback_by_message'] = {
                    row['chat_message_id']: {k: row[k] for k in ('rating', 'reason', 'comment')}
                    for row in Feedback.objects.filter(user=user, chat_message__session=instance)
                    .values('chat_message_id', 'rating', 'reason', 'comment')
                }
            except Exception:  # noqa: BLE001 — feedback must not fail the transcript
                self.context['feedback_by_message'] = {}
        return super().to_representation(instance)

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

    def validate_autonomy(self, value):
        """ask | auto | plan. `full` stays an agent-builder choice — chat never
        offers a mode that asks about nothing."""
        level = (value or '').strip().lower()
        if level not in ('ask', 'auto', 'plan'):
            raise serializers.ValidationError(
                'Autonomy must be ask, auto or plan.'
            )
        return level

    def validate_title(self, value):
        title = value.strip()
        if not title:
            raise serializers.ValidationError("Title is required and cannot be blank.")
        return title


class ChatSessionListSerializer(ChatSessionSerializer):
    """A session as the history list shows it: everything but the transcript.

    The list used to be `ChatSessionSerializer`, whose nested `messages` put
    every message of every listed conversation — each with its attachments — in
    one response, one query per session plus one per message, to render a
    sidebar that reads `id` and `title`. The transcript is the detail route's
    job, and both clients already fetch it there when a conversation is opened.
    Subclassed rather than restated so a field added to the session stays in
    the list too.
    """

    class Meta(ChatSessionSerializer.Meta):
        fields = [f for f in ChatSessionSerializer.Meta.fields if f != 'messages']
        read_only_fields = [
            f for f in ChatSessionSerializer.Meta.read_only_fields if f != 'messages'
        ]
