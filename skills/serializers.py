from rest_framework import serializers
from .models import Skill

MAX_SKILL_CONTENT_WORDS = 10_000

def validate_skill_content(value):
    if len(value.split()) > MAX_SKILL_CONTENT_WORDS:
        raise serializers.ValidationError(
            f'Skill content must be at most {MAX_SKILL_CONTENT_WORDS} words.'
        )
    return value

class SkillSerializer(serializers.ModelSerializer):
    author = serializers.CharField(source='author_name', read_only=True)
    isShared = serializers.BooleanField(source='is_shared', default=False)
    updatedAt = serializers.DateTimeField(source='updated_at', read_only=True)
    content = serializers.CharField(validators=[validate_skill_content])
    
    class Meta:
        model = Skill
        fields = ['id', 'title', 'description', 'content', 'author', 'isShared', 'category', 'updatedAt']
        read_only_fields = ['id', 'author', 'updatedAt']

    def create(self, validated_data):
        return super().create(validated_data)

class SkillSearchSerializer(serializers.Serializer):
    """Serializer for skill search parameters."""
    query = serializers.CharField(required=False, allow_blank=True, default='')
    category = serializers.CharField(required=False, allow_null=True)
    tab = serializers.ChoiceField(choices=['mine', 'public'], default='mine')
    page = serializers.IntegerField(default=1, min_value=1)
    page_size = serializers.IntegerField(default=12, min_value=1, max_value=100)
