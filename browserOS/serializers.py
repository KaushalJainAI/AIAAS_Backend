from rest_framework import serializers
from .models import OSWorkspace, OSAppWindow, OSNotification

class OSAppWindowSerializer(serializers.ModelSerializer):
    class Meta:
        model = OSAppWindow
        fields = '__all__'
        read_only_fields = ['workspace', 'created_at', 'updated_at']

class OSWorkspaceSerializer(serializers.ModelSerializer):
    windows = OSAppWindowSerializer(many=True, read_only=True)

    class Meta:
        model = OSWorkspace
        fields = ['id', 'name', 'theme_preferences', 'windows', 'created_at', 'updated_at']
        read_only_fields = ['id', 'created_at', 'updated_at']

class OSNotificationSerializer(serializers.ModelSerializer):
    class Meta:
        model = OSNotification
        fields = ['id', 'title', 'message', 'type', 'is_read', 'created_at']
        read_only_fields = ['id', 'created_at']
