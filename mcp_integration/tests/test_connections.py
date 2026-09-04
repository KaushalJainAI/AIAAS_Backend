"""
Regression tests for the Connections surface.

The bug these exist for: the UI rendered an enable/disable toggle on curated
system servers, and `PATCH /api/mcp/servers/<id>/` answered 403 because
`_assert_owner` (correctly) refuses edits to shared rows. Enable/disable is now
per-user state in `MCPServerPreference`, so the toggle works without letting one
account mutate a template every other account reads.
"""
from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from mcp_integration.client import _servers_for_user_sync
from mcp_integration.models import MCPServer, MCPServerPreference

User = get_user_model()


class SystemServerToggleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='x')
        self.other = User.objects.create_user(username='bob', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.system = MCPServer.objects.create(
            name='Curated Thing',
            display_name='Curated Thing',
            type='stdio',
            command='npx',
            category='utilities',
            tagline='Does a curated thing',
            icon_slug='curated-thing',
            user=None,
        )

    def _live_ids(self, user):
        return {s.id for s in _servers_for_user_sync(user.id)}

    def test_disabling_a_system_server_no_longer_403s(self):
        res = self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': False}, format='json'
        )
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['effective_enabled'])

    def test_disabling_does_not_mutate_the_shared_row(self):
        self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': False}, format='json'
        )
        self.system.refresh_from_db()
        self.assertTrue(self.system.enabled)
        self.assertTrue(
            MCPServerPreference.objects.filter(
                user=self.user, server=self.system, enabled=False
            ).exists()
        )

    def test_disabling_hides_the_server_from_the_agent_tool_list(self):
        """A cosmetic toggle is worse than none: the agent would keep the tool."""
        self.assertIn(self.system.id, self._live_ids(self.user))
        self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': False}, format='json'
        )
        self.assertNotIn(self.system.id, self._live_ids(self.user))

    def test_one_users_choice_does_not_leak_to_another(self):
        self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': False}, format='json'
        )
        self.assertNotIn(self.system.id, self._live_ids(self.user))
        self.assertIn(self.system.id, self._live_ids(self.other))

    def test_re_enabling_restores_the_server(self):
        self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': False}, format='json'
        )
        res = self.client.post(
            f'/api/mcp/servers/{self.system.id}/set-enabled/',
            {'enabled': True},
            format='json',
        )
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.data['effective_enabled'])
        self.assertIn(self.system.id, self._live_ids(self.user))

    def test_a_globally_disabled_server_stays_disabled_for_everyone(self):
        """
        The property is unchanged; the way it is reported is not. This used to
        answer 200 with `effective_enabled: false` — a success code for a write
        that could not take effect, which the page rendered as a switch flipping
        back with a toast saying "turned off". It is now a 409, and the
        preference row is not written at all. See PlatformDisabledRowTests.
        """
        self.system.enabled = False
        self.system.save(update_fields=['enabled'])
        res = self.client.post(
            f'/api/mcp/servers/{self.system.id}/set-enabled/',
            {'enabled': True},
            format='json',
        )
        self.assertEqual(res.status_code, 409)
        self.assertNotIn(self.system.id, self._live_ids(self.user))

    # ---- the 403 must survive for everything that is genuinely an edit ----

    def test_config_edits_to_a_system_server_are_still_refused(self):
        res = self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'command': 'rm'}, format='json'
        )
        self.assertEqual(res.status_code, 403)
        self.system.refresh_from_db()
        self.assertEqual(self.system.command, 'npx')

    def test_an_edit_smuggled_alongside_enabled_is_refused(self):
        """`enabled` must not become a wrapper that carries edits past the check."""
        res = self.client.patch(
            f'/api/mcp/servers/{self.system.id}/',
            {'enabled': False, 'command': 'rm'},
            format='json',
        )
        self.assertEqual(res.status_code, 403)
        self.system.refresh_from_db()
        self.assertEqual(self.system.command, 'npx')

    def test_deleting_a_system_server_is_still_refused(self):
        res = self.client.delete(f'/api/mcp/servers/{self.system.id}/')
        self.assertEqual(res.status_code, 403)
        self.assertTrue(MCPServer.objects.filter(id=self.system.id).exists())

    def test_a_non_boolean_enabled_is_rejected(self):
        res = self.client.patch(
            f'/api/mcp/servers/{self.system.id}/', {'enabled': 'banana'}, format='json'
        )
        self.assertEqual(res.status_code, 400)

    def test_another_users_private_server_is_not_visible(self):
        theirs = MCPServer.objects.create(
            name='Bob private', type='stdio', command='npx', user=self.other
        )
        res = self.client.post(
            f'/api/mcp/servers/{theirs.id}/set-enabled/', {'enabled': False}, format='json'
        )
        self.assertEqual(res.status_code, 404)


class OwnedServerToggleTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='carol', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.mine = MCPServer.objects.create(
            name='My Server', type='stdio', command='npx', user=self.user
        )

    def test_toggling_an_owned_server_writes_the_row_itself(self):
        res = self.client.post(
            f'/api/mcp/servers/{self.mine.id}/set-enabled/', {'enabled': False}, format='json'
        )
        self.assertEqual(res.status_code, 200)
        self.mine.refresh_from_db()
        self.assertFalse(self.mine.enabled)
        # No preference row: there is no shared template to shadow.
        self.assertFalse(MCPServerPreference.objects.filter(server=self.mine).exists())

    def test_patch_still_edits_an_owned_server(self):
        res = self.client.patch(
            f'/api/mcp/servers/{self.mine.id}/', {'command': 'python'}, format='json'
        )
        self.assertEqual(res.status_code, 200)
        self.mine.refresh_from_db()
        self.assertEqual(self.mine.command, 'python')


class ConnectionPresentationTests(TestCase):
    """The list response must carry everything the UI needs to render a card."""

    def setUp(self):
        self.user = User.objects.create_user(username='dave', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def test_curated_servers_carry_presentation_metadata(self):
        server = MCPServer.objects.create(
            name='Gmail', display_name='Gmail', type='stdio', command='npx',
            category='google_workspace', tagline='Read, search, and send emails',
            icon_slug='gmail', help_url='https://example.com', user=None,
        )
        res = self.client.get('/api/mcp/servers/')
        # Select by id, not by name. `unique_together = ('name', 'user')` does
        # not constrain curated rows at all — two NULLs are distinct — so the
        # migrations' own Gmail row is also in this response, and matching on
        # the name asserted against whichever of the two sorted first.
        row = next(s for s in res.data['servers'] if s['id'] == server.id)
        self.assertEqual(row['label'], 'Gmail')
        self.assertEqual(row['category'], 'google_workspace')
        self.assertEqual(row['icon_slug'], 'gmail')
        self.assertTrue(row['tagline'])
        self.assertTrue(row['is_system'])
        self.assertTrue(row['effective_enabled'])

    def test_label_falls_back_to_name(self):
        server = MCPServer.objects.create(
            name='Unbranded', type='stdio', command='npx', user=self.user
        )
        self.assertEqual(server.label, 'Unbranded')

    def test_env_is_never_returned(self):
        MCPServer.objects.create(
            name='Secretive', type='stdio', command='npx',
            env={'TOKEN': 'shh'}, user=self.user,
        )
        res = self.client.get('/api/mcp/servers/')
        row = next(s for s in res.data['servers'] if s['name'] == 'Secretive')
        self.assertNotIn('env', row)


class PlatformDisabledRowTests(TestCase):
    """
    A curated row the platform turned off is not a switch the user has.

    `_visible_servers_queryset` filters `enabled=True` before it subtracts the
    user's "off" preferences, so a preference of True over a disabled row can
    never make the connection live. The endpoint used to store it anyway and
    answer 200 with `effective_enabled: false` — which the page rendered as a
    switch that snapped back under a toast reading "turned off".
    """

    def setUp(self):
        self.user = User.objects.create_user(username='carol', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.user)
        self.server = MCPServer.objects.create(
            name='Platform Off',
            display_name='Platform Off',
            type='stdio',
            command='npx',
            args=['-y', 'nope'],
            user=None,
            enabled=False,
            setup_notes='No server for this exists yet.',
        )
        self.url = f'/api/mcp/servers/{self.server.id}/set-enabled/'

    def test_turning_on_a_platform_disabled_row_is_refused(self):
        response = self.client.post(self.url, {'enabled': True}, format='json')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.data['code'], 'server_unavailable')

    def test_the_refusal_carries_the_reason_the_row_gives(self):
        """The note is the only thing that tells a user why the card is dead."""
        response = self.client.post(self.url, {'enabled': True}, format='json')
        self.assertEqual(response.data['detail'], 'No server for this exists yet.')

    def test_no_unreachable_preference_row_is_written(self):
        self.client.post(self.url, {'enabled': True}, format='json')
        self.assertFalse(
            MCPServerPreference.objects.filter(
                user=self.user, server=self.server
            ).exists(),
            'A preference that cannot take effect must not be stored.',
        )

    def test_turning_it_off_is_still_allowed(self):
        """
        Off-over-off is redundant but harmless, and refusing it would make the
        endpoint's contract depend on state the caller cannot see.
        """
        response = self.client.post(self.url, {'enabled': False}, format='json')
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data['effective_enabled'])

    def test_a_disabled_row_never_reaches_the_runtime(self):
        """The whole reason the preference is pointless — pinned here so the
        refusal above cannot be 'fixed' by making the queryset looser."""
        self.assertNotIn(self.server, _servers_for_user_sync(self.user.id))
