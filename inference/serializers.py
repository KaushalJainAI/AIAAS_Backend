from rest_framework import serializers
from .models import Document

class DocumentSerializer(serializers.ModelSerializer):
    """
    Serializer for Document model.
    Maintains existing API contract with field mappings.
    """
    title = serializers.CharField(source='name', read_only=True)
    filename = serializers.CharField(source='name', read_only=True)
    author_name = serializers.SerializerMethodField()
    content = serializers.CharField(source='content_text', read_only=True)

    class Meta:
        model = Document
        fields = [
            'id', 'title', 'filename', 'file_type', 'file_size', 
            'chunk_count', 'is_shared', 'shared_at', 'created_at', 
            'updated_at', 'sharing_mode', 'status', 'author_name',
            'content', 'metadata'
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'chunk_count']

    def get_author_name(self, obj):
        return obj.user.get_full_name() or obj.user.username

class RagSearchSerializer(serializers.Serializer):
    """Serializer for RAG search parameters."""
    query = serializers.CharField(required=True)
    top_k = serializers.IntegerField(default=5, min_value=1, max_value=50)
    include_platform = serializers.BooleanField(default=False)

class RagQuerySerializer(serializers.Serializer):
    """Serializer for RAG query parameters."""
    question = serializers.CharField(required=True)
    llm_type = serializers.CharField(default='openai')
    credential_id = serializers.UUIDField(required=False, allow_null=True)
    top_k = serializers.IntegerField(default=5, min_value=1, max_value=50)
