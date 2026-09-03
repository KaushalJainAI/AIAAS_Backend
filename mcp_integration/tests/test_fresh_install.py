"""
Does a brand-new install actually work?

Django's test runner builds its database by running every migration, so a test
that asserts on catalog state here is asserting exactly what a fresh deploy gets
from `migrate` — with no management command run afterwards. That was the gap:
curated MCP servers arrived by migration while the credential types they reference
arrived only from `manage.py seed_connector_credentials`, so a deploy that skipped
the command had a catalog where nothing credentialed could ever connect, and
nothing logged an error.

Note these tests deliberately do NOT call the seeder. If one of them starts
failing, migrations alone are no longer sufficient — which is the regression.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from credentials.models import CredentialType

from mcp_integration.credential_injector import SETTINGS_SLUG
from mcp_integration.models import MCPServer

User = get_user_model()


class FreshInstallCatalogTests(TestCase):
    def test_curated_servers_exist(self):
        self.assertGreater(MCPServer.objects.filter(user__isnull=True).count(), 0)

    def test_credential_types_exist_without_running_the_seeder(self):
        self.assertGreater(CredentialType.objects.count(), 0)

    def test_every_credential_type_a_curated_server_requires_is_present(self):
        """The failure this file exists for: catalog present, its types absent."""
        seeded = set(CredentialType.objects.values_list('slug', flat=True))
        missing = {}
        for server in MCPServer.objects.filter(user__isnull=True):
            absent = [
                slug
                for slug in (server.required_credential_types or [])
                if slug != SETTINGS_SLUG and slug not in seeded
            ]
            if absent:
                missing[server.name] = absent
        self.assertEqual(missing, {}, f"unseeded credential types: {missing}")

    def test_every_curated_mapping_resolves_to_a_real_field(self):
        oauth_columns = {'access_token', 'refresh_token'}
        fields_by_slug = {
            ct.slug: {f.get('name') for f in (ct.fields_schema or [])}
            for ct in CredentialType.objects.all()
        }
        problems = []
        for server in MCPServer.objects.filter(user__isnull=True):
            for env_key, mapping in (server.credential_env_map or {}).items():
                slug, _, field = str(mapping).partition(':')
                if slug == SETTINGS_SLUG:
                    continue
                known = fields_by_slug.get(slug)
                if known is None:
                    problems.append(f"{server.name}.{env_key}: no type '{slug}'")
                elif field not in known and field not in oauth_columns:
                    problems.append(
                        f"{server.name}.{env_key}: '{slug}' has no field '{field}'"
                    )
        self.assertEqual(problems, [], '\n'.join(problems))

    def test_curated_servers_carry_the_metadata_the_ui_renders(self):
        """A server with no icon_slug or tagline renders as an unlabelled tile."""
        bare = [
            s.name
            for s in MCPServer.objects.filter(user__isnull=True)
            if not s.icon_slug or not s.tagline or s.category == 'custom'
        ]
        self.assertEqual(bare, [], f"missing presentation metadata: {bare}")


class FreshInstallConnectionsPageTests(TestCase):
    """The page a new user lands on must be usable, not just non-empty."""

    def setUp(self):
        self.user = User.objects.create_user(username='newcomer', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_the_page_can_render_every_curated_connection(self):
        res = self.client.get('/api/mcp/servers/')
        self.assertEqual(res.status_code, 200)
        rows = res.data['servers']
        self.assertGreater(len(rows), 0)
        for row in rows:
            self.assertTrue(row['label'], row)
            self.assertIn('effective_enabled', row)
            self.assertIn('category', row)

    def test_a_new_user_can_connect_every_credentialed_connection(self):
        """
        For each connection needing auth, the type it names must be resolvable
        *and* offer fields to fill — otherwise Connect opens nothing, which is
        what happened when a server pointed at a type the seeder never created.
        """
        types_by_slug = {ct.slug: ct for ct in CredentialType.objects.all()}
        unconnectable = []
        for row in self.client.get('/api/mcp/servers/').data['servers']:
            for slug in row.get('required_credential_types') or []:
                ct = types_by_slug.get(slug)
                if ct is None:
                    unconnectable.append(f"{row['label']}: no type '{slug}'")
                elif ct.auth_method != 'oauth2' and not ct.fields_schema:
                    unconnectable.append(
                        f"{row['label']}: type '{slug}' has no fields and is not OAuth"
                    )
        self.assertEqual(unconnectable, [], '\n'.join(unconnectable))

    def test_built_in_connections_need_no_setup(self):
        """Something must work on day one, before the user connects anything."""
        ready = [
            row
            for row in self.client.get('/api/mcp/servers/').data['servers']
            if not row.get('required_credential_types') and row['effective_enabled']
        ]
        self.assertGreater(len(ready), 0)


class CuratedPackageTests(TestCase):
    """
    Every *enabled* curated connector must name a package that exists.

    The catalogue shipped with six npm specs that were never published
    (`@modelcontextprotocol/server-fetch`, `@modelcontextprotocol/server-notion`,
    `@gptscript-ai/google-workspace-mcp` on four rows). Nothing caught it,
    because the endpoint that would have reported it timed out before npm
    finished saying 404.

    These assertions are offline on purpose — a test that queried the registry
    would be flaky and would fail in CI without network. They lock in the
    packages that were verified by hand (started, tool list read back), so
    changing one is a deliberate edit here rather than a silent repoint.
    """

    # package spec -> tools it advertised when verified.
    VERIFIED = {
        "@modelcontextprotocol/server-filesystem": 14,
        "@modelcontextprotocol/server-memory": 9,
        "@modelcontextprotocol/server-sequential-thinking": 1,
        "@modelcontextprotocol/server-slack": 8,
        "@notionhq/notion-mcp-server": 24,
        "@tokenizin/mcp-npx-fetch": 4,
        # Verified 2026-08-31 with a real MCP handshake (initialize +
        # tools/list) and only CLIENT_ID / CLIENT_SECRET / REFRESH_TOKEN in the
        # environment. `0011` recorded this package as hanging; that reading
        # came from running the binary bare, which every stdio server does.
        "@shinzolabs/gmail-mcp": 64,
        # Verified 2026-08-31 by handshake. Both carry credential *files*
        # rather than env vars — see `credential_file_map`.
        "@isaacphi/mcp-gdrive": 4,        # Drive and Sheets share this package
        "@cocal/google-calendar-mcp": 13,
    }

    # Packages proven not to exist or not to start. A row must never point here.
    KNOWN_BROKEN = {
        "@modelcontextprotocol/server-fetch",
        "@modelcontextprotocol/server-notion",
        "@gptscript-ai/google-workspace-mcp",
    }

    def _specs(self, *, enabled):
        for server in MCPServer.objects.filter(user__isnull=True, enabled=enabled):
            spec = next(
                (a for a in (server.args or []) if not a.startswith("-")), None
            )
            if spec is not None:
                yield server.name, spec

    def test_enabled_connectors_name_a_verified_package(self):
        for name, spec in self._specs(enabled=True):
            with self.subTest(server=name):
                self.assertIn(
                    spec, self.VERIFIED,
                    f"{name} points at {spec!r}, which has not been verified to "
                    f"start. Start it and read back its tool list, then add it "
                    f"to VERIFIED — or leave the row disabled.",
                )

    def test_no_enabled_connector_points_at_a_known_broken_package(self):
        for name, spec in self._specs(enabled=True):
            with self.subTest(server=name):
                self.assertNotIn(spec, self.KNOWN_BROKEN)

    def test_slack_maps_the_workspace_id_it_cannot_start_without(self):
        # "Please set SLACK_BOT_TOKEN and SLACK_TEAM_ID environment variables"
        # is fatal at startup, so a token-only mapping could never connect.
        slack = MCPServer.objects.get(name="Slack", user__isnull=True)
        self.assertEqual(
            set(slack.credential_env_map),
            {"SLACK_BOT_TOKEN", "SLACK_TEAM_ID"},
        )

    def test_notion_maps_the_variable_the_official_server_reads(self):
        # The official server reads NOTION_TOKEN; the old NOTION_API_KEY is
        # ignored, so the connection would start and then see nothing.
        notion = MCPServer.objects.get(name="Notion", user__isnull=True)
        self.assertEqual(set(notion.credential_env_map), {"NOTION_TOKEN"})

    def test_a_disabled_connector_explains_itself(self):
        for server in MCPServer.objects.filter(user__isnull=True, enabled=False):
            with self.subTest(server=server.name):
                self.assertTrue(
                    server.setup_notes.strip(),
                    "A connector offered but switched off must say why.",
                )


class UpcomingConnectorTests(TestCase):
    """Notion and Slack are announced, not connectable.

    The pairing is the whole point: `coming_soon` is presentation, `enabled`
    is access. A flag that drifted apart from the switch would put a
    "Coming soon" badge on a connector agents could still call.
    """

    UPCOMING = ("Notion", "Slack")

    def test_upcoming_connectors_are_still_listed(self):
        # Withdrawn, not hidden: the catalogue queryset does not filter on
        # `enabled`, which is what lets the page announce them at all.
        for name in self.UPCOMING:
            with self.subTest(server=name):
                self.assertTrue(
                    MCPServer.objects.filter(name=name, user__isnull=True).exists()
                )

    def test_upcoming_connectors_are_withheld_not_merely_labelled(self):
        for name in self.UPCOMING:
            with self.subTest(server=name):
                server = MCPServer.objects.get(name=name, user__isnull=True)
                self.assertTrue(server.coming_soon)
                self.assertFalse(
                    server.enabled,
                    "coming_soon is presentation only; `enabled` is what "
                    "actually keeps the tools out of an agent's toolbox.",
                )

    def test_no_connector_claims_coming_soon_while_enabled(self):
        # The pairing, as a rule rather than two spot checks: any future row
        # that sets one without the other is the bug this guards.
        offenders = list(
            MCPServer.objects.filter(coming_soon=True, enabled=True)
            .values_list("name", flat=True)
        )
        self.assertEqual(
            offenders, [],
            f"These rows are labelled 'Coming soon' but still live: {offenders}",
        )

    def test_upcoming_connectors_say_so_in_their_notes(self):
        # The card renders `setup_notes` as the reason it is inert; notes that
        # only describe setup would read as instructions the user can act on.
        for name in self.UPCOMING:
            with self.subTest(server=name):
                server = MCPServer.objects.get(name=name, user__isnull=True)
                self.assertIn("Coming soon", server.setup_notes)

    def test_upcoming_connectors_are_invisible_to_agents(self):
        # The end that matters: an announced connector must not still be
        # resolvable as a tool source.
        from mcp_integration.client import _visible_servers_queryset

        visible = set(
            _visible_servers_queryset(None, enabled_only=True)
            .values_list("name", flat=True)
        )
        for name in self.UPCOMING:
            with self.subTest(server=name):
                self.assertNotIn(name, visible)
