from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.core.exceptions import PermissionDenied
from django.test import SimpleTestCase, TestCase

from mcp_integration.client import MCPClientManager
from mcp_integration.models import MCPServer
from mcp_integration.tool_cache import MCPToolCache
from mcp_integration.tool_provider import encode_tool_name


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


class MCPToolCacheTests(TestCase):
    def setUp(self):
        cache.clear()
        # Real rows rather than invented ids. `set` now writes through to
        # `MCPToolCatalogue` as well as to Redis, and that row carries real
        # foreign keys so a catalogue dies with the connection or the user it
        # was derived from. Production always passes ids that exist —
        # `list_tools` has already resolved the server and coerced the user —
        # so using fictional ones here tested a path no caller can reach.
        User = get_user_model()
        self.owner = User.objects.create_user(username="cache-owner", password="pw")
        self.server = MCPServer.objects.create(
            name="Scoped", type="stdio", command="npx",
        )

    def tearDown(self):
        cache.clear()

    def test_tool_cache_is_scoped_by_user(self):
        async_to_sync(MCPToolCache.set)(
            self.server.id, self.owner.id, [{"name": "private"}],
        )

        self.assertEqual(
            async_to_sync(MCPToolCache.get)(self.server.id, self.owner.id),
            [{"name": "private"}],
        )
        self.assertIsNone(
            async_to_sync(MCPToolCache.get)(self.server.id, self.owner.id + 1)
        )
        self.assertIsNone(async_to_sync(MCPToolCache.get)(self.server.id, None))

    def test_scoping_survives_a_cache_miss(self):
        """The durable tier must not widen what the cache narrowed.

        A second tier under the cache is only safe if it answers the same
        question: were it keyed by server alone, a Redis eviction would start
        handing one user's tool list — resolved with their credentials — to
        everybody else on that connection.
        """
        async_to_sync(MCPToolCache.set)(
            self.server.id, self.owner.id, [{"name": "private"}],
        )
        cache.clear()

        self.assertEqual(
            async_to_sync(MCPToolCache.get)(self.server.id, self.owner.id),
            [{"name": "private"}],
        )
        self.assertIsNone(
            async_to_sync(MCPToolCache.get)(self.server.id, self.owner.id + 1)
        )


class MCPToolNameTests(SimpleTestCase):
    def test_encoded_tool_names_are_schema_safe_bounded_and_collision_resistant(self):
        dotted = encode_tool_name(123, "github.create_issue")
        underscored = encode_tool_name(123, "github_create_issue")
        long_name = encode_tool_name(123, "x" * 200)

        self.assertRegex(dotted, r"^[a-zA-Z0-9_-]{1,64}$")
        self.assertLessEqual(len(long_name), 64)
        self.assertNotEqual(dotted, underscored)
