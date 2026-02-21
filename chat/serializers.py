from rest_framework import serializers
from .models import ChatSession, ChatMessage

class ChatMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'content', 'metadata', 'created_at']

class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = ChatSession
        fields = [
            'id', 'title', 'llm_provider', 'llm_model', 
            'system_prompt', 'created_at', 'updated_at', 'messages'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'messages']
