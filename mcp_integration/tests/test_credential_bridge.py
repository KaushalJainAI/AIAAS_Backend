"""
Tests for the two gaps that made curated connectors fail after a successful
"connect": credentials whose secrets live in the OAuth token columns, and
mappings whose value belongs to the platform rather than to the user.
"""
from asgiref.sync import async_to_sync
from cryptography.fernet import Fernet
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from credentials.models import Credential, CredentialType

from mcp_integration.credential_injector import (
    CredentialInjector,
    CredentialInvalidError,
    CredentialMissingError,
)
from mcp_integration.models import MCPServer

User = get_user_model()


def _encrypt(value: str) -> bytes:
    return Fernet(Credential._get_encryption_key()).encrypt(value.encode())


class OAuthTokenColumnBridgeTests(TestCase):
    """
    The OAuth callback writes `refresh_token` to a dedicated column, while
    `get_credential_data()` reads the `encrypted_data` blob. Field lookup has to
    see both or connecting by OAuth yields a credential the injector calls empty.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='ollie', password='x')
        # update_or_create: `credentials.0005` seeds a real `google-oauth2` row
        # when the test database migrates, and these tests need its schema pinned
        # to exactly the field under test.
        self.cred_type, _ = CredentialType.objects.update_or_create(
            slug='google-oauth2',
            defaults={
                'name': 'Google OAuth2', 'auth_method': 'oauth2',
                'fields_schema': [{'name': 'refresh_token', 'label': 'Refresh token',
                                   'type': 'password', 'required': True}],
            },
        )
        self.server = MCPServer.objects.create(
            name='Gmail', type='stdio', command='npx',
            required_credential_types=['google-oauth2'],
            credential_env_map={'GOOGLE_REFRESH_TOKEN': 'google-oauth2:refresh_token'},
            user=None,
        )

    def test_a_token_stored_only_in_the_column_still_resolves(self):
        cred = Credential(
            user=self.user, credential_type=self.cred_type, name='Google Account',
            refresh_token=_encrypt('rt-from-oauth'),
        )
        cred.set_credential_data({})
        cred.save()

        resolved = async_to_sync(CredentialInjector.resolve)(self.server, self.user)
        self.assertEqual(resolved.env_vars['GOOGLE_REFRESH_TOKEN'], 'rt-from-oauth')

    def test_a_blob_field_wins_over_the_column(self):
        """A hand-entered value must not be shadowed by a stale token column."""
        cred = Credential(
            user=self.user, credential_type=self.cred_type, name='Google Account',
            refresh_token=_encrypt('stale-column'),
        )
        cred.set_credential_data({'refresh_token': 'hand-entered'})
        cred.save()

        resolved = async_to_sync(CredentialInjector.resolve)(self.server, self.user)
        self.assertEqual(resolved.env_vars['GOOGLE_REFRESH_TOKEN'], 'hand-entered')

    def test_a_credential_with_neither_source_reports_the_missing_field(self):
        cred = Credential(
            user=self.user, credential_type=self.cred_type, name='Google Account',
        )
        cred.set_credential_data({})
        cred.save()

        with self.assertRaises(CredentialInvalidError) as ctx:
            async_to_sync(CredentialInjector.resolve)(self.server, self.user)
        self.assertIn('refresh_token', str(ctx.exception))

    def test_no_credential_at_all_still_reports_missing_credential(self):
        with self.assertRaises(CredentialMissingError):
            async_to_sync(CredentialInjector.resolve)(self.server, self.user)


@override_settings(
    GOOGLE_OAUTH_CLIENT_ID='platform-client-id',
    GOOGLE_OAUTH_CLIENT_SECRET='platform-client-secret',
)
class PlatformSettingsSourceTests(TestCase):
    """
    `@settings:VAR` lets a mapping pull a platform-owned value. Without it every
    Google connector demanded that the user create their own GCP OAuth client.
    """

    def setUp(self):
        self.user = User.objects.create_user(username='pia', password='x')
        # update_or_create: `credentials.0005` seeds a real `google-oauth2` row
        # when the test database migrates, and these tests need its schema pinned
        # to exactly the field under test.
        self.cred_type, _ = CredentialType.objects.update_or_create(
            slug='google-oauth2',
            defaults={
                'name': 'Google OAuth2', 'auth_method': 'oauth2',
                'fields_schema': [{'name': 'refresh_token', 'label': 'Refresh token',
                                   'type': 'password', 'required': True}],
            },
        )
        cred = Credential(
            user=self.user, credential_type=self.cred_type, name='Google Account',
        )
        cred.set_credential_data({'refresh_token': 'rt'})
        cred.save()

    def _server(self, env_map, required=('google-oauth2',)):
        return MCPServer.objects.create(
            name=f'Srv {id(env_map)}', type='stdio', command='npx',
            required_credential_types=list(required),
            credential_env_map=env_map,
            user=None,
        )

    def test_settings_values_are_injected_without_a_user_credential(self):
        server = self._server({
            'GOOGLE_CLIENT_ID': '@settings:GOOGLE_OAUTH_CLIENT_ID',
            'GOOGLE_CLIENT_SECRET': '@settings:GOOGLE_OAUTH_CLIENT_SECRET',
            'GOOGLE_REFRESH_TOKEN': 'google-oauth2:refresh_token',
        })
        resolved = async_to_sync(CredentialInjector.resolve)(server, self.user)
        self.assertEqual(resolved.env_vars, {
            'GOOGLE_CLIENT_ID': 'platform-client-id',
            'GOOGLE_CLIENT_SECRET': 'platform-client-secret',
            'GOOGLE_REFRESH_TOKEN': 'rt',
        })

    def test_the_sentinel_is_never_looked_up_as_a_user_credential(self):
        server = self._server(
            {'GOOGLE_CLIENT_ID': '@settings:GOOGLE_OAUTH_CLIENT_ID'},
            required=('@settings',),
        )
        resolved = async_to_sync(CredentialInjector.resolve)(server, self.user)
        self.assertEqual(resolved.env_vars['GOOGLE_CLIENT_ID'], 'platform-client-id')

    def test_settings_values_work_in_header_templates(self):
        server = MCPServer.objects.create(
            name='SSE srv', type='sse', url='https://example.com/sse',
            credential_header_map={'Authorization': 'Bearer {@settings:GOOGLE_OAUTH_CLIENT_ID}'},
            user=None,
        )
        resolved = async_to_sync(CredentialInjector.resolve)(server, self.user)
        self.assertEqual(resolved.headers['Authorization'], 'Bearer platform-client-id')

    @override_settings(GOOGLE_OAUTH_CLIENT_ID='')
    def test_an_unconfigured_setting_names_the_setting_not_the_user(self):
        """The operator has to fix this, so the error must not blame a credential."""
        server = self._server({'GOOGLE_CLIENT_ID': '@settings:GOOGLE_OAUTH_CLIENT_ID'})
        with self.assertRaises(CredentialInvalidError) as ctx:
            async_to_sync(CredentialInjector.resolve)(server, self.user)
        message = str(ctx.exception)
        self.assertIn('GOOGLE_OAUTH_CLIENT_ID', message)
        self.assertIn('Platform setting', message)


class CuratedCatalogIntegrityTests(TestCase):
    """
    Guards the class of bug that shipped six broken connectors: a mapping naming
    a credential field that the credential type does not define. It cannot fail
    at connect time, only at run time, so it needs a test rather than a reviewer.
    """

    OAUTH_COLUMNS = {'access_token', 'refresh_token'}

    def setUp(self):
        # Migrations seed credential types too (credentials.0005), so this is
        # belt-and-braces. It is kept deliberately: seeding explicitly here means
        # this class only ever fails for a genuine *mapping* mistake, while
        # tests_fresh_install is the one that fails if migrations stop being
        # sufficient. Two failures, two distinct causes.
        from django.core.management import call_command

        call_command('seed_connector_credentials', verbosity=0)

    def test_every_curated_mapping_names_a_field_that_exists(self):
        from mcp_integration.credential_injector import SETTINGS_SLUG

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
                    problems.append(
                        f"{server.name}: {env_key} needs credential type "
                        f"'{slug}', which is not seeded"
                    )
                elif field not in known and field not in self.OAUTH_COLUMNS:
                    problems.append(
                        f"{server.name}: {env_key} reads '{slug}:{field}', but "
                        f"that type only defines {sorted(known)}"
                    )
        self.assertEqual(problems, [], '\n'.join(problems))
