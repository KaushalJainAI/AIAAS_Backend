import logging
import asyncio

from asgiref.sync import async_to_sync
from django.db.models import Q
from rest_framework import permissions, status, viewsets
from rest_framework.decorators import action
from django.core.exceptions import PermissionDenied as DjangoPermissionDenied
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from drf_spectacular.utils import extend_schema, extend_schema_view, OpenApiResponse

from .client import LIST_TOOLS_TIMEOUT, MCPClientManager, MCPConnectionError
from .credential_injector import (
    CredentialInjector,
    CredentialInvalidError,
    CredentialMissingError,
)
from .models import MCPServer, MCPServerPreference
from .serializers import MCPServerSerializer
from .tool_cache import MCPToolCache

logger = logging.getLogger(__name__)

_TRUE = {True, "true", "True", "1", 1}
_FALSE = {False, "false", "False", "0", 0}


def _message(exc: BaseException, fallback: str | None = None) -> str:
    """A human-readable reason that is never the empty string."""
    return str(exc).strip() or fallback or exc.__class__.__name__


def _coerce_bool(value) -> bool | None:
    """Return the boolean `value` denotes, or None if it denotes neither."""
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


@extend_schema_view(
    list=extend_schema(
        responses={200: OpenApiResponse(response={
            "type": "object",
            "required": ["servers"],
            "properties": {
                "servers": {"type": "array", "items": {"$ref": "#/components/schemas/MCPServer"}},
            },
        })},
    ),
)
class MCPServerViewSet(viewsets.ModelViewSet):
    """
    CRUD for MCP servers + tool discovery.

    A user sees their own servers plus any system-wide servers (user=NULL).
    Only the owner can update/delete their servers. System-wide servers are
    read-only *config*, but every user may still enable/disable one for
    themselves — that choice is stored per user in `MCPServerPreference`.
    """
    serializer_class = MCPServerSerializer
    permission_classes = [permissions.IsAuthenticated]
    pagination_class = None
    queryset = MCPServer.objects.all()  # for DRF router introspection

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return MCPServer.objects.none()
        return MCPServer.objects.filter(
            Q(user=user) | Q(user__isnull=True)
        ).order_by("name")

    def get_serializer_context(self):
        """
        Add the requesting user's explicit "off" choices so `effective_enabled`
        resolves without a query per server.
        """
        context = super().get_serializer_context()
        user = self.request.user
        if user.is_authenticated:
            context["disabled_server_ids"] = set(
                MCPServerPreference.objects.filter(user=user, enabled=False)
                .values_list("server_id", flat=True)
            )
        return context

    def list(self, request, *args, **kwargs):
        """Return wrapped list of servers, matching credentials pattern."""
        queryset = self.filter_queryset(self.get_queryset())
        serializer = self.get_serializer(queryset, many=True)
        return Response({'servers': serializer.data})

    def update(self, request, *args, **kwargs):
        """
        Config edits require ownership, but enable/disable does not.

        A request that only toggles `enabled` on a system-wide server is a
        per-user preference, not an edit to the shared template, so it is routed
        to `MCPServerPreference` instead of being refused. Everything else on a
        system server still 403s via `_assert_owner`.
        """
        server = self.get_object()
        if server.user_id is None and set(request.data.keys()) == {"enabled"}:
            return self._write_preference(server, request.data["enabled"])
        # Assert ownership *before* field validation: a system server is
        # forbidden to edit (403), which must win over any 400 the new command
        # allowlist would raise on the smuggled fields.
        self._assert_owner(server)
        return super().update(request, *args, **kwargs)

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

    # ---- Enable / disable ----

    def _write_preference(self, server: MCPServer, raw_enabled) -> Response:
        """Persist a per-user enable/disable choice and drop the tool cache."""
        enabled = _coerce_bool(raw_enabled)
        if enabled is None:
            return Response(
                {"enabled": "Must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        MCPServerPreference.objects.update_or_create(
            user=self.request.user,
            server=server,
            defaults={"enabled": enabled},
        )
        async_to_sync(MCPToolCache.invalidate)(server.id, self.request.user.id)
        serializer = self.get_serializer(server)
        data = dict(serializer.data)
        # The context set was computed before this write, so report the new state
        # directly rather than serving a value we just invalidated.
        data["effective_enabled"] = enabled and server.enabled
        return Response(data)

    @action(detail=True, methods=["post"], url_path="set-enabled")
    def set_enabled(self, request, pk=None):
        """
        Turn a connection on or off for the current user.

        Works uniformly for system and owned servers, which is what lets the UI
        render one toggle without knowing who owns the row.
        """
        server = self.get_object()
        enabled = _coerce_bool(request.data.get("enabled"))
        if enabled is None:
            return Response(
                {"enabled": "Must be a boolean."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if server.user_id is not None:
            # Owned servers keep their state on the row itself; there is no one
            # else to shadow it from.
            server.enabled = enabled
            server.save(update_fields=["enabled", "updated_at"])
            async_to_sync(MCPToolCache.invalidate)(server.id, server.user_id)
            return Response(self.get_serializer(server).data)
        return self._write_preference(server, enabled)

    # ---- Tool discovery / credential diagnostics ----

    @action(detail=True, methods=["get"])
    def tools(self, request, pk=None):
        """
        List tools available on this server (cached).

        Every failure answers with a `code` and a non-empty `error`, because
        this is the one thing on the Connections page a non-technical user can
        act on: "package not found" and "your token expired" need different
        actions, and both used to arrive as the same bare 502. `str()` on the
        exception is not enough on its own — `str(TimeoutError())` is the empty
        string, which is exactly what the original 502 body carried.
        """
        server = self.get_object()
        try:
            manager = MCPClientManager(server.id, user=request.user)

            async def list_with_timeout():
                return await asyncio.wait_for(
                    manager.list_tools(), timeout=LIST_TOOLS_TIMEOUT
                )

            tools = async_to_sync(list_with_timeout)()
        except CredentialMissingError as e:
            return self._tools_error(e, "credential_missing", status.HTTP_400_BAD_REQUEST)
        except CredentialInvalidError as e:
            return self._tools_error(e, "credential_invalid", status.HTTP_400_BAD_REQUEST)
        except (PermissionDenied, DjangoPermissionDenied):
            # `get_server_config` re-checks visibility at connect time and
            # raises Django's flavour, which DRF also renders as 403. Both must
            # escape the broad handler below, or losing access to a server would
            # be reported as that server being down.
            raise
        except (asyncio.TimeoutError, TimeoutError) as e:
            logger.warning("Timed out listing tools for MCP server %s", server.id)
            return self._tools_error(
                e, "connection_timeout", status.HTTP_504_GATEWAY_TIMEOUT,
                fallback=(
                    f"'{server.name}' did not respond within "
                    f"{LIST_TOOLS_TIMEOUT:.0f}s."
                ),
            )
        except MCPConnectionError as e:
            logger.warning("Could not connect to MCP server %s: %s", server.id, e)
            return self._tools_error(e, "connection_failed", status.HTTP_502_BAD_GATEWAY)
        except Exception as e:  # noqa: BLE001 — a third-party server's failure
            # is a 502 about that server, never a 500 about this one.
            logger.exception("Failed to list tools for server %s", server.id)
            return self._tools_error(e, "connection_failed", status.HTTP_502_BAD_GATEWAY)
        return Response({"tools": tools, "server_id": server.id, "server_name": server.name})

    @staticmethod
    def _tools_error(exc: BaseException, code: str, http_status: int,
                     fallback: str | None = None) -> Response:
        return Response(
            {"error": _message(exc, fallback), "code": code}, status=http_status
        )

    @action(detail=True, methods=["get"])
    def validate_credentials(self, request, pk=None):
        """Dry-run credential resolution for this server."""
        server = self.get_object()
        try:
            errors = async_to_sync(CredentialInjector.validate)(server, request.user)
        except Exception as e:  # noqa: BLE001 — a diagnostic that 500s tells the
            # user nothing; report the failure as the diagnosis it is.
            logger.exception("Credential validation failed for server %s", server.id)
            return Response({"ok": False, "errors": [_message(e)]})
        return Response({"ok": not errors, "errors": errors})

    @action(detail=False, methods=["get"], url_path="tools")
    def all_tools(self, request):
        """
        Aggregate of tools from every server visible to the user.

        `get_all_tools_from_all_servers` already degrades a broken server to an
        empty list, so this only guards the aggregate itself failing — the whole
        page must not go down because one connector is misconfigured.
        """
        from .client import get_all_tools_from_all_servers

        try:
            tools = async_to_sync(get_all_tools_from_all_servers)(request.user)
        except Exception as e:  # noqa: BLE001
            logger.exception("Failed to aggregate MCP tools for user %s", request.user.id)
            return Response({"tools": [], "error": _message(e), "code": "aggregate_failed"})
        return Response({"tools": tools})
