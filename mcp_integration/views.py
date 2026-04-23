import logging

from asgiref.sync import async_to_sync
from django.db.models import Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from .client import MCPClientManager
from .credential_injector import (
    CredentialInjector,
    CredentialInvalidError,
    CredentialMissingError,
)
from .models import MCPServer
from .serializers import MCPServerSerializer
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)


class MCPServerViewSet(viewsets.ModelViewSet):
    """
    CRUD for MCP servers + tool discovery.

    A user sees their own servers plus any system-wide servers (user=NULL).
    Only the owner can update/delete their servers; system-wide servers are
    read-only via the API.
    """
    serializer_class = MCPServerSerializer
    permission_classes = [permissions.IsAuthenticated]
    queryset = MCPServer.objects.all()  # for DRF router introspection

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return MCPServer.objects.none()
        return MCPServer.objects.filter(
            Q(user=user) | Q(user__isnull=True)
        ).order_by("name")

    def list(self, request, *args, **kwargs):
        """Return wrapped list of servers, matching credentials pattern."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'servers': serializer.data})

    def perform_create(self, serializer):
        serializer.save(user=self.request.user)

    def _assert_owner(self, server: MCPServer):
        if server.user_id is None:
            raise PermissionDenied("System-wide MCP servers cannot be modified via the API.")
        if server.user_id != self.request.user.id:
            raise PermissionDenied("You do not own this MCP server.")

    def perform_update(self, serializer):
        self._assert_owner(serializer.instance)
        serializer.save()
        async_to_sync(MCPToolCache.invalidate)(serializer.instance.id, serializer.instance.user_id)

    def perform_destroy(self, instance):
        self._assert_owner(instance)
        async_to_sync(MCPToolCache.invalidate)(instance.id, instance.user_id)
        instance.delete()

    # ---- Tool discovery / credential diagnostics ----

    @action(detail=True, methods=["get"])
    def tools(self, request, pk=None):
        """List tools available on this server (cached)."""
        server = self.get_object()
        try:
            manager = MCPClientManager(server.id, user=request.user)
            tools = async_to_sync(manager.list_tools)()
        except CredentialMissingError as e:
            return Response({"error": str(e), "code": "credential_missing"}, status=status.HTTP_400_BAD_REQUEST)
        except CredentialInvalidError as e:
            return Response({"error": str(e), "code": "credential_invalid"}, status=status.HTTP_400_BAD_REQUEST)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to list tools for server %s", server.id)
            return Response({"error": str(e), "code": "connection_failed"}, status=status.HTTP_502_BAD_GATEWAY)
        return Response({"tools": tools, "server_id": server.id, "server_name": server.name})

    @action(detail=True, methods=["get"])
    def validate_credentials(self, request, pk=None):
        """Dry-run credential resolution for this server."""
        server = self.get_object()
        errors = async_to_sync(CredentialInjector.validate)(server, request.user)
        return Response({"ok": not errors, "errors": errors})

    @action(detail=False, methods=["get"], url_path="tools")
    def all_tools(self, request):
        """Aggregate of tools from every server visible to the user."""
        from .client import get_all_tools_from_all_servers

        tools = async_to_sync(get_all_tools_from_all_servers)(request.user)
        return Response({"tools": tools})
