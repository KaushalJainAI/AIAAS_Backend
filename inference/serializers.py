from rest_framework import serializers
from .models import Document, KnowledgeBase


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    size_human = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeBase
        fields = [
            'id', 'name', 'description', 'embedding_model',
            'vector_dim', 'doc_count', 'vector_count',
            'index_size_bytes', 'size_human', 'is_default',
            'created_at', 'updated_at',
        ]
        read_only_fields = [
            'id', 'embedding_model', 'vector_dim',
            'doc_count', 'vector_count', 'index_size_bytes',
            'is_default', 'created_at', 'updated_at',
        ]

    def get_size_human(self, obj):
        b = obj.index_size_bytes
        for unit in ('B', 'KB', 'MB', 'GB'):
            if b < 1024:
                return f'{b:.1f} {unit}'
            b /= 1024
        return f'{b:.1f} TB'


class DocumentSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='name', read_only=True)
    filename = serializers.CharField(source='name', read_only=True)
    author_name = serializers.SerializerMethodField()
    content = serializers.CharField(source='content_text', read_only=True)
    knowledge_base_id = serializers.IntegerField(source='knowledge_base.id', read_only=True, allow_null=True)
    knowledge_base_name = serializers.CharField(source='knowledge_base.name', read_only=True, allow_null=True)

    class Meta:
        model = Document
        fields = [
            'id', 'title', 'filename', 'file_type', 'file_size',
            'chunk_count', 'is_shared', 'shared_at', 'created_at',
            'updated_at', 'sharing_mode', 'status', 'author_name',
            'content', 'metadata', 'knowledge_base_id', 'knowledge_base_name',
            'error_message',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'chunk_count']

    def get_author_name(self, obj):
        return obj.user.get_full_name() or obj.user.username


class RagSearchSerializer(serializers.Serializer):
    query = serializers.CharField(required=True)
    top_k = serializers.IntegerField(default=5, min_value=1, max_value=50)
    include_platform = serializers.BooleanField(default=False)
    kb_id = serializers.IntegerField(required=False, allow_null=True)


class RagQuerySerializer(serializers.Serializer):
    question = serializers.CharField(required=True)
    llm_type = serializers.CharField(default='openai')
    credential_id = serializers.UUIDField(required=False, allow_null=True)
    top_k = serializers.IntegerField(default=5, min_value=1, max_value=50)
    kb_id = serializers.IntegerField(required=False, allow_null=True)
