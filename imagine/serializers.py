from rest_framework import serializers

from .models import Generation, ImagineConversation, ImagineMessage
from .services.capabilities import capabilities_for


class GenerationSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Generation
        fields = [
            'id', 'user_email', 'type', 'prompt', 'negative_prompt',
            'model', 'resolution', 'aspect_ratio', 'duration', 'seed',
            'quality', 'output_format', 'generate_audio', 'voice', 'speed',
            'output_url', 'job_id', 'polling_url', 'status', 'error_message', 'metadata',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user_email', 'output_url', 'job_id', 'polling_url',
            'status', 'error_message', 'created_at', 'updated_at',
            # Written by the dispatcher (generation cost, agent reasoning), so
            # a client cannot seed it with arbitrary content.
            'metadata',
        ]

    def validate(self, attrs):
        """Reject a model the selected modality cannot actually run.

        Without this the request reaches OpenRouter and fails there, surfacing
        as an opaque provider error on a row already marked failed. Catching it
        at the edge returns a 400 naming the problem instead.
        """
        kind = attrs.get('type') or getattr(self.instance, 'type', None)
        model = attrs.get('model') or getattr(self.instance, 'model', None)
        if not kind or not model:
            return attrs

        caps = capabilities_for(self.context.get('request').user) if self.context.get('request') else None
        if not caps:
            return attrs
        available = {m['id'] for m in caps.get(kind) or []}
        # An empty bucket means the catalog itself is unavailable (no
        # credential, OpenRouter unreachable). Don't turn that into a
        # validation error about the user's model choice.
        if available and model not in available:
            raise serializers.ValidationError({
                'model': f"'{model}' is not an available {kind} model.",
            })
        return attrs


class ImagineMessageSerializer(serializers.ModelSerializer):
    generation = GenerationSerializer(read_only=True)

    class Meta:
        model = ImagineMessage
        fields = ['id', 'role', 'content', 'intent', 'generation', 'requires_hitl', 'created_at']
        read_only_fields = fields


class ImagineConversationSerializer(serializers.ModelSerializer):
    last_message = serializers.SerializerMethodField()

    class Meta:
        model = ImagineConversation
        fields = ['id', 'title', 'status', 'pending_intent', 'created_at', 'updated_at', 'last_message']
        read_only_fields = fields

    def get_last_message(self, obj):
        # The conversation viewset annotates these to avoid an N+1 per row;
        # the fallback keeps this serializer correct when used standalone.
        content = getattr(obj, 'last_message_content', None)
        role = getattr(obj, 'last_message_role', None)
        if content is None:
            msg = obj.messages.order_by('-created_at').first()
            if not msg:
                return None
            return {'role': msg.role, 'content': msg.content[:200]}
        return {'role': role, 'content': content[:200]}


class ImagineConversationDetailSerializer(serializers.ModelSerializer):
    messages = ImagineMessageSerializer(many=True, read_only=True)

    class Meta:
        model = ImagineConversation
        fields = ['id', 'title', 'status', 'pending_intent', 'created_at', 'updated_at', 'messages']
        read_only_fields = fields
