from rest_framework import serializers

from .models import Generation, ImagineConversation, ImagineMessage


class GenerationSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Generation
        fields = [
            'id', 'user_email', 'type', 'prompt', 'negative_prompt',
            'model', 'resolution', 'aspect_ratio', 'duration', 'seed',
            'motion_intensity', 'fps', 'voice', 'speed',
            'output_url', 'job_id', 'polling_url', 'status', 'error_message', 'metadata',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'user_email', 'output_url', 'job_id', 'polling_url',
            'status', 'error_message', 'created_at', 'updated_at',
        ]

    def create(self, validated_data):
        return super().create(validated_data)


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
        msg = obj.messages.order_by('-created_at').first()
        if not msg:
            return None
        return {'role': msg.role, 'content': msg.content[:200]}


class ImagineConversationDetailSerializer(serializers.ModelSerializer):
    messages = ImagineMessageSerializer(many=True, read_only=True)

    class Meta:
        model = ImagineConversation
        fields = ['id', 'title', 'status', 'pending_intent', 'created_at', 'updated_at', 'messages']
        read_only_fields = fields
