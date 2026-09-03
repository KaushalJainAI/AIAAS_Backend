from rest_framework import serializers

from .models import Generation, ImagineConversation, ImagineMessage
from .services.capabilities import capabilities_for
from .services.catalog import find_model
from .validation import DialError, validate_dials


class GenerationSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Generation
        fields = [
            'id', 'user_email', 'type', 'prompt', 'negative_prompt',
            'model', 'resolution', 'aspect_ratio', 'size', 'duration', 'seed',
            'quality', 'output_format', 'background', 'output_compression',
            'batch_size', 'reference_urls', 'frame_images',
            'generate_audio', 'voice', 'speed', 'instructions', 'response_format',
            'output_url', 'output_urls', 'job_id', 'polling_url', 'status',
            'error_message', 'metadata',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user_email', 'output_url', 'output_urls', 'job_id',
            'polling_url',
            'status', 'error_message', 'created_at', 'updated_at',
            # Written by the dispatcher (generation cost, agent reasoning), so
            # a client cannot seed it with arbitrary content.
            'metadata',
        ]

    def validate(self, attrs):
        """Reject a model the modality cannot run, and a dial the model cannot take.

        Without the first check the request reaches OpenRouter and fails there,
        surfacing as an opaque provider error on a row already marked failed.

        The second is the same argument one level down. Each model advertises
        its own dial set, and the two ways of getting it wrong do not look
        alike: a value outside an advertised enum is a hard 400 from the
        provider, while a dial the model never advertised is *silently
        ignored* — the request succeeds, the user is billed, and the setting
        did nothing. Both are refused here, against the same catalogue the
        picker renders from. See `imagine/validation.py`.
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

        merged = {
            field: attrs.get(field, getattr(self.instance, field, None))
            for field in (
                'resolution', 'aspect_ratio', 'size', 'duration', 'quality',
                'output_format', 'background', 'output_compression',
                'batch_size', 'reference_urls', 'frame_images', 'voice',
                'speed', 'instructions', 'response_format',
            )
        }
        try:
            validate_dials(kind, find_model(caps, kind, model), merged)
        except DialError as exc:
            raise serializers.ValidationError(exc.args[0])
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
