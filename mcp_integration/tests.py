from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase

from .client import MCPClientManager
from .models import MCPServer
from .tool_cache import MCPToolCache
from .tool_provider import encode_tool_name
from .workflow_validator import validate_mcp_nodes


class MCPAccessControlTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user(username="owner", password="pw")
        self.other = User.objects.create_user(username="other", password="pw")
        self.server = MCPServer.objects.create(
            name="Private MCP",
            type="stdio",
            command="node",
            user=self.owner,
        )

    def test_client_manager_rejects_servers_owned_by_other_users(self):
        manager = MCPClientManager(self.server.id, user=self.other.id)

        with self.assertRaises(PermissionDenied):
            async_to_sync(manager.get_server_config)()

    def test_workflow_validator_rejects_servers_owned_by_other_users(self):
        workflow = {
            "nodes": [
                {
                    "type": "mcp_tool",
                    "config": {"server_id": self.server.id, "tool_name": "search"},
                }
            ]
        }

        errors = async_to_sync(validate_mcp_nodes)(workflow, self.other.id)

        self.assertEqual(
            errors,
            [f"Workflow references unavailable MCP server (id={self.server.id})."],
        )


class MCPToolCacheTests(TestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_tool_cache_is_scoped_by_user(self):
        async_to_sync(MCPToolCache.set)(1, 10, [{"name": "private"}])

        self.assertEqual(async_to_sync(MCPToolCache.get)(1, 10), [{"name": "private"}])
        self.assertIsNone(async_to_sync(MCPToolCache.get)(1, 11))
        self.assertIsNone(async_to_sync(MCPToolCache.get)(1, None))


class MCPToolNameTests(SimpleTestCase):
    def test_encoded_tool_names_are_schema_safe_bounded_and_collision_resistant(self):
        dotted = encode_tool_name(123, "github.create_issue")
        underscored = encode_tool_name(123, "github_create_issue")
        long_name = encode_tool_name(123, "x" * 200)

        self.assertRegex(dotted, r"^[a-zA-Z0-9_-]{1,64}$")
        self.assertLessEqual(len(long_name), 64)
        self.assertNotEqual(dotted, underscored)
