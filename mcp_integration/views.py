from rest_framework import viewsets, permissions
from rest_framework.decorators import action
from rest_framework.response import Response
from .models import MCPServer
from .serializers import MCPServerSerializer
from .client import MCPClientManager

class MCPServerViewSet(viewsets.ModelViewSet):
    queryset = MCPServer.objects.all()
    serializer_class = MCPServerSerializer
    permission_classes = [permissions.IsAuthenticated]

    def get_queryset(self):
        # Filtering by user if needed, for system servers return all?
        # For now, return all servers.
        return MCPServer.objects.filter(enabled=True)

    @action(detail=True, methods=['get'])
    def tools(self, request, pk=None):
        """List tools available on this server."""
        try:
             # Sync wrapper for async tool listing
             # DRF views are sync by default, but we can use adrf or asgiref
             # If using sync Django, we must bridge async.
             import asyncio
             from asgiref.sync import async_to_sync
             
             manager = MCPClientManager(pk)
             # async_to_sync requires an event loop.
             tools = async_to_sync(manager.list_tools)()
             return Response(tools)
        except Exception as e:
             return Response({"error": str(e)}, status=500)
