from rest_framework import serializers
from .models import MCPServer

class MCPServerSerializer(serializers.ModelSerializer):
    class Meta:
        model = MCPServer
        fields = '__all__'
