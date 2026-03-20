from rest_framework import serializers
from .models import ChatSession, ChatMessage, ChatAttachment


class ChatAttachmentSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChatAttachment
        fields = ['id', 'filename', 'file_type', 'file_size', 'created_at']
        read_only_fields = ['id', 'created_at']


class ChatMessageSerializer(serializers.ModelSerializer):
    attachments = ChatAttachmentSerializer(many=True, read_only=True)

    class Meta:
        model = ChatMessage
        fields = ['id', 'role', 'content', 'message_type', 'metadata', 'attachments', 'created_at']


class ChatSessionSerializer(serializers.ModelSerializer):
    messages = ChatMessageSerializer(many=True, read_only=True)
    
    class Meta:
        model = ChatSession
        fields = [
            'id', 'title', 'llm_provider', 'llm_model', 
            'system_prompt', 'total_tokens_used',
            'created_at', 'updated_at', 'messages'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'messages', 'total_tokens_used']
