from rest_framework import serializers
from .models import Generation

class GenerationSerializer(serializers.ModelSerializer):
    user_email = serializers.EmailField(source='user.email', read_only=True)

    class Meta:
        model = Generation
        fields = [
            'id', 'user_email', 'type', 'prompt', 'negative_prompt', 
            'model', 'resolution', 'aspect_ratio', 'duration', 'seed',
            'motion_intensity', 'fps', 'voice', 'speed', 
            'output_url', 'job_id', 'polling_url', 'status', 'error_message', 'metadata',
            'created_at', 'updated_at'
        ]
        read_only_fields = ['id', 'user_email', 'output_url', 'job_id', 'polling_url', 'status', 'error_message', 'created_at', 'updated_at']

    def create(self, validated_data):
        # The user is added in the perform_create method of the view
        return super().create(validated_data)
