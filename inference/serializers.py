from rest_framework import serializers

from .models import (
    Document, ExtractedRow, ExtractionSchema, Folder, KnowledgeBase,
)

ALLOWED_FIELD_TYPES = {'string', 'number', 'date', 'currency', 'boolean'}


class KnowledgeBaseSerializer(serializers.ModelSerializer):
    size_human = serializers.SerializerMethodField()

    class Meta:
        model = KnowledgeBase
        fields = [
            'id', 'name', 'description', 'backend', 'embedding_model',
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

    def validate_name(self, value):
        value = (value or '').strip()
        if not value:
            raise serializers.ValidationError('A knowledge base needs a name.')
        # The (user, name) uniqueness lives in a Meta constraint, which DRF's
        # UniqueValidator never sees — without this a duplicate became a 500.
        request = self.context.get('request')
        owner = getattr(request, 'user', None)
        if owner is not None and getattr(owner, 'is_authenticated', False):
            qs = KnowledgeBase.objects.filter(user=owner, name=value)
            if self.instance is not None:
                qs = qs.exclude(pk=self.instance.pk)
            if qs.exists():
                raise serializers.ValidationError(
                    f"You already have a knowledge base named '{value}'."
                )
        return value

    def validate_backend(self, value):
        valid = {choice[0] for choice in KnowledgeBase.BACKEND_CHOICES}
        if value not in valid:
            raise serializers.ValidationError(
                f"'{value}' is not one of {', '.join(sorted(valid))}."
            )
        if self.instance is not None:
            existing = getattr(self.instance, 'backend', 'vector')
            # Switching machinery under ingested documents would orphan
            # whatever the old backend stored — vectors without an index,
            # postings without chunks. An empty KB may switch freely.
            if (
                value != existing
                and self.instance.documents.exists()
            ):
                raise serializers.ValidationError(
                    'This knowledge base already contains documents. Its '
                    'retrieval backend can only be changed while it is empty.'
                )
        return value


class FolderSerializer(serializers.ModelSerializer):
    """One folder as the API presents it.

    `path` is exposed but holds *ids*, and is never accepted back — the API is
    id-addressed end to end, because a path string is the traversal shape this
    design exists to avoid. Clients render `breadcrumbs` instead.
    """

    parent_id = serializers.IntegerField(read_only=True, allow_null=True)
    child_count = serializers.SerializerMethodField()
    document_count = serializers.SerializerMethodField()

    class Meta:
        model = Folder
        fields = [
            'id', 'name', 'parent_id', 'path', 'depth',
            'child_count', 'document_count', 'created_at', 'updated_at',
        ]
        read_only_fields = fields

    # Both counts already exclude trashed rows without asking: a related
    # manager takes the *default* manager's class, which is LiveManager.
    # The list view annotates these to avoid N+1; the fallback is for detail.
    def get_child_count(self, obj):
        annotated = getattr(obj, 'child_count_annotated', None)
        return annotated if annotated is not None else obj.children.count()

    def get_document_count(self, obj):
        annotated = getattr(obj, 'document_count_annotated', None)
        return annotated if annotated is not None else obj.documents.count()


class FolderWriteSerializer(serializers.Serializer):
    """Create and update bodies. Deliberately not a ModelSerializer.

    `parent_id` must be resolved through `filesystem.resolve_folder` against the
    caller, and a ModelSerializer's default queryset for a FK would happily
    accept any folder in the table. Keeping the write shape explicit keeps that
    resolution in the view, where the choke point lives.
    """

    name = serializers.CharField(required=False, allow_blank=False, max_length=255)
    parent_id = serializers.IntegerField(required=False, allow_null=True)


class MoveSerializer(serializers.Serializer):
    folder_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)
    document_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)
    target_folder_id = serializers.IntegerField(required=False, allow_null=True)

    def validate(self, attrs):
        if not attrs.get('folder_ids') and not attrs.get('document_ids'):
            raise serializers.ValidationError('Nothing to move.')
        return attrs


class RestoreSerializer(serializers.Serializer):
    folder_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)
    document_ids = serializers.ListField(
        child=serializers.IntegerField(), required=False, default=list)

    def validate(self, attrs):
        if not attrs.get('folder_ids') and not attrs.get('document_ids'):
            raise serializers.ValidationError('Nothing to restore.')
        return attrs


class DocumentSerializer(serializers.ModelSerializer):
    title = serializers.CharField(source='name', read_only=True)
    filename = serializers.CharField(source='name', read_only=True)
    author_name = serializers.SerializerMethodField()
    content = serializers.CharField(source='content_text', read_only=True)
    knowledge_base_id = serializers.IntegerField(source='knowledge_base.id', read_only=True, allow_null=True)
    knowledge_base_name = serializers.CharField(source='knowledge_base.name', read_only=True, allow_null=True)
    folder_id = serializers.IntegerField(read_only=True, allow_null=True)
    folder_path = serializers.SerializerMethodField()

    class Meta:
        model = Document
        fields = [
            'id', 'title', 'filename', 'file_type', 'file_size',
            'chunk_count', 'is_shared', 'shared_at', 'created_at',
            'updated_at', 'sharing_mode', 'status', 'author_name',
            'content', 'metadata', 'knowledge_base_id', 'knowledge_base_name',
            'error_message', 'folder_id', 'folder_path',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at', 'chunk_count']

    def get_author_name(self, obj):
        return obj.user.get_full_name() or obj.user.username

    def get_folder_path(self, obj):
        """Human-readable location, derived on read.

        `folder_id` is the locator; this is for display only. A stored name
        path is what would make rename O(descendants) and tempt someone into
        accepting a path string as a locator.
        """
        if obj.folder_id is None:
            return '/'
        from .filesystem import name_path
        return name_path(obj.folder)


class DocumentListSerializer(DocumentSerializer):
    """Compact document list serializer; detail endpoints include content."""

    class Meta(DocumentSerializer.Meta):
        fields = [
            'id', 'title', 'filename', 'file_type', 'file_size',
            'chunk_count', 'is_shared', 'shared_at', 'created_at',
            'updated_at', 'sharing_mode', 'status', 'author_name',
            'metadata', 'knowledge_base_id', 'knowledge_base_name',
            'error_message', 'folder_id', 'folder_path',
        ]


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


class ExtractedRowSerializer(serializers.ModelSerializer):
    schema_name = serializers.CharField(source='schema.name', read_only=True)

    class Meta:
        model = ExtractedRow
        fields = [
            'id', 'document_name', 'document', 'data', 'field_confidence',
            'confidence', 'status', 'schema_name', 'reviewed_at', 'created_at',
        ]
        read_only_fields = ['id', 'status', 'schema_name', 'reviewed_at', 'created_at']

    def validate_document(self, value):
        """The row's source document must belong to the caller.

        `document` is a writable FK on an otherwise owner-scoped resource, so
        without this a user could point their own row at anyone's document —
        the row is the audit trail for "who cleared this value", and an
        unverifiable source makes that answer worthless.
        """
        if value is None:
            return value
        request = self.context.get('request')
        owner = getattr(request, 'user', None)
        if owner is None or not getattr(owner, 'is_authenticated', False):
            raise serializers.ValidationError(
                'Cannot attach a document without an authenticated caller.'
            )
        if value.user_id != owner.id:
            raise serializers.ValidationError('That document is not yours.')
        return value


class ExtractionSchemaSerializer(serializers.ModelSerializer):
    field_count = serializers.IntegerField(read_only=True)
    row_count = serializers.IntegerField(read_only=True)
    review_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = ExtractionSchema
        fields = [
            'id', 'name', 'description', 'fields', 'field_count',
            'source_kind', 'source_ref', 'confidence_threshold',
            'llm_model', 'row_count', 'review_count', 'created_at', 'updated_at',
        ]
        read_only_fields = ['id', 'created_at', 'updated_at']

    def validate_confidence_threshold(self, value):
        if not 0 <= value <= 1:
            raise serializers.ValidationError('Confidence is a fraction between 0 and 1.')
        return value

    def validate_llm_model(self, value):
        """A pinned model must exist in the registry.

        Caught here rather than at run time: an unregistered model still runs
        (the registry can lag a provider's catalogue), so a typo used to be
        discovered only as an opaque provider error, once per document, across
        a whole batch.
        """
        value = (value or '').strip()
        if not value:
            return value
        from llm.models import AIModel
        if not AIModel.objects.filter(value=value).exists():
            raise serializers.ValidationError(
                f"'{value}' is not a known model. Leave blank for the default."
            )
        return value

    def validate_fields(self, value):
        if not isinstance(value, list) or not value:
            raise serializers.ValidationError('A schema needs at least one field.')

        names = []
        for i, field in enumerate(value):
            if not isinstance(field, dict):
                raise serializers.ValidationError(f'fields[{i}] must be an object.')
            name = (field.get('name') or '').strip()
            if not name:
                raise serializers.ValidationError(f'fields[{i}] needs a name.')
            ftype = field.get('type', 'string')
            if ftype not in ALLOWED_FIELD_TYPES:
                raise serializers.ValidationError(
                    f"fields[{i}].type '{ftype}' is not one of {', '.join(sorted(ALLOWED_FIELD_TYPES))}."
                )
            names.append(name)

        # Duplicate names would silently overwrite each other in the row's data
        # dict, losing a column with no error anywhere.
        dupes = {n for n in names if names.count(n) > 1}
        if dupes:
            raise serializers.ValidationError(f"Duplicate field names: {', '.join(sorted(dupes))}.")
        return value
