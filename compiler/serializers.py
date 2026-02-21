from rest_framework import serializers

class WorkflowDefinitionSerializer(serializers.Serializer):
    """Serializer for basic workflow structure (nodes and edges)."""
    nodes = serializers.ListField(child=serializers.DictField(), required=True)
    edges = serializers.ListField(child=serializers.DictField(), required=True)
    settings = serializers.DictField(required=False, default=dict)

class CompilationResultSerializer(serializers.Serializer):
    """Serializer for compilation output (success, errors, warnings)."""
    success = serializers.BooleanField()
    errors = serializers.ListField(child=serializers.DictField())
    warnings = serializers.ListField(child=serializers.DictField())
    execution_plan = serializers.DictField()
    stats = serializers.DictField()
