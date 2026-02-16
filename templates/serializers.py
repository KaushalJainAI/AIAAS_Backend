from rest_framework import serializers
from .models import WorkflowTemplate, WorkflowRating, WorkflowBookmark, TemplateComment

class WorkflowRatingSerializer(serializers.ModelSerializer):
    user_name = serializers.ReadOnlyField(source='user.username')
    
    class Meta:
        model = WorkflowRating
        fields = ['id', 'user_name', 'stars', 'review', 'created_at']

class WorkflowBookmarkSerializer(serializers.ModelSerializer):
    class Meta:
        model = WorkflowBookmark
        fields = ['id', 'template', 'created_at']

class TemplateCommentSerializer(serializers.ModelSerializer):
    user_name = serializers.ReadOnlyField(source='user.username')
    replies = serializers.SerializerMethodField()
    
    class Meta:
        model = TemplateComment
        fields = ['id', 'user_name', 'text', 'parent', 'replies', 'created_at']
        
    def get_replies(self, obj):
        if obj.replies.exists():
            return TemplateCommentSerializer(obj.replies.all(), many=True).data
        return []

class WorkflowTemplateSerializer(serializers.ModelSerializer):
    is_bookmarked = serializers.SerializerMethodField()
    user_rating = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkflowTemplate
        fields = [
            'id', 'name', 'description', 'category', 'tags', 
            'nodes', 'edges', 'workflow_settings', 'author_name',
            'status', 'success_rate', 'usage_count', 'fork_count',
            'average_rating', 'rating_count', 'is_featured',
            'is_bookmarked', 'user_rating', 'created_at'
        ]
        read_only_fields = [
            'success_rate', 'usage_count', 'fork_count', 
            'average_rating', 'rating_count', 'created_at'
        ]

    def get_is_bookmarked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return WorkflowBookmark.objects.filter(template=obj, user=request.user).exists()
        return False

    def get_user_rating(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            rating = WorkflowRating.objects.filter(template=obj, user=request.user).first()
            if rating:
                return rating.stars
        return None

class TemplateListItemSerializer(serializers.ModelSerializer):
    is_bookmarked = serializers.SerializerMethodField()
    
    class Meta:
        model = WorkflowTemplate
        fields = [
            'id', 'name', 'description', 'category', 'tags', 'author_name',
            'status', 'success_rate', 'usage_count', 'fork_count',
            'average_rating', 'rating_count', 'is_featured', 'is_bookmarked'
        ]

    def get_is_bookmarked(self, obj):
        request = self.context.get('request')
        if request and request.user.is_authenticated:
            return WorkflowBookmark.objects.filter(template=obj, user=request.user).exists()
        return False
