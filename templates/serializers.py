from rest_framework import serializers
from .models import WorkflowTemplate

class WorkflowTemplateSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowTemplate
        fields = [
            'id', 'name', 'description', 'category', 'tags', 
            'nodes', 'edges', 'workflow_settings',
            'status', 'success_rate', 'usage_count', 'created_at'
        ]
        read_only_fields = ['success_rate', 'usage_count', 'created_at']

class TemplateListItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowTemplate
        fields = [
            'id', 'name', 'description', 'category', 'tags', 
            'status', 'success_rate', 'usage_count'
        ]
