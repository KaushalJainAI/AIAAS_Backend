from django.test import TestCase
from django.contrib.auth import get_user_model
from unittest.mock import MagicMock, patch, AsyncMock
from asgiref.sync import async_to_sync
from credentials.verification import CredentialVerifier
from credentials.models import Credential, CredentialType

User = get_user_model()

class CredentialVerifierTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        
        # Dummy types, pinned to the exact shape these tests verify against.
        # update_or_create rather than create: `credentials.0005` seeds the real
        # catalog during test-database migration, so several of these slugs
        # already exist and `create` would hit the unique constraint on `slug`.
        # Overwriting keeps each test's premise explicit rather than depending on
        # whatever the seeder happens to define.
        def _type(slug, **defaults):
            obj, _ = CredentialType.objects.update_or_create(
                slug=slug, defaults=defaults
            )
            return obj

        self.type_openai = _type(
            'openai', name='OpenAI', auth_method='api_key',
            fields_schema=[{'name': 'apiKey', 'required': True}],
        )
        self.type_slack = _type(
            'slack', name='Slack', auth_method='bearer',
            fields_schema=[{'name': 'token', 'required': True}],
        )
        self.type_google = _type(
            'google-oauth2', name='Google', auth_method='oauth2',
            oauth_config={'auth_url': 'https://accounts.google.com',
                          'token_url': 'https://oauth2.googleapis.com/token'},
        )
        self.type_custom = _type(
            'website-login', name='Website', auth_method='custom',
            fields_schema=[{'name': 'loginUrl', 'required': True}],
        )

    @patch('requests.get')
    def test_verify_api_key_openai_success(self, mock_get):
        cred = Credential(user=self.user, credential_type=self.type_openai, name="Test OpenAI")
        # Mocking get_credential_data returning decrypted dict
        cred.get_credential_data = MagicMock(return_value={'apiKey': 'sk-123'})
        
        mock_get.return_value.status_code = 200
        
        # Patch the internal async method with AsyncMock
        with patch('credentials.verification.CredentialVerifier._verify_api_key', new_callable=AsyncMock) as mock_verify_key:
             mock_verify_key.return_value = (True, "Successfully connected")
             valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
        
        self.assertTrue(valid)
        self.assertIn("Successfully connected", msg)

    @patch('requests.get')
    def test_verify_api_key_openai_failure(self, mock_get):
        cred = Credential(user=self.user, credential_type=self.type_openai, name="Fail OpenAI")
        cred.get_credential_data = MagicMock(return_value={'apiKey': 'sk-bad'})
        
        # Patch the internal async method with AsyncMock
        with patch('credentials.verification.CredentialVerifier._verify_api_key', new_callable=AsyncMock) as mock_verify_key:
             mock_verify_key.return_value = (False, "Invalid API Key")
             valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
             
        self.assertFalse(valid)
        self.assertIn("Invalid API Key", msg)

    @patch('requests.post')
    def test_verify_bearer_slack(self, mock_post):
        cred = Credential(user=self.user, credential_type=self.type_slack, name="Slack Test")
        cred.get_credential_data = MagicMock(return_value={'token': 'xoxb-123'})
        
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'ok': True, 'user': 'bot', 'team': 'AIAAS'}
        
        with patch('credentials.verification.CredentialVerifier._verify_bearer', new_callable=AsyncMock) as mock_verify_token:
             mock_verify_token.return_value = (True, "Connected as bot")
             valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
             
        self.assertTrue(valid)
        self.assertIn("Connected as bot", msg)

    def test_verify_oauth2_missing_config(self):
        # Create type with missing config
        type_bad = CredentialType.objects.create(name='Bad OAuth', slug='bad', auth_method='oauth2')
        cred = Credential(user=self.user, credential_type=type_bad, name="Bad OAuth")
        cred.get_credential_data = MagicMock(return_value={})
        
        valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
        self.assertFalse(valid)
        self.assertIn("Invalid Configuration", msg)

    @patch('credentials.models.Credential.get_valid_access_token')
    @patch('requests.get')
    def test_verify_google_oauth2_success(self, mock_get, mock_token):
        cred = Credential(user=self.user, credential_type=self.type_google, name="Google Test")
        cred.get_credential_data = MagicMock(return_value={})
        mock_token.return_value = "access-token-123"
        
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'test@gmail.com'}
        
        mock_get.return_value.status_code = 200
        mock_get.return_value.json.return_value = {'email': 'test@gmail.com'}
        
        with patch('credentials.verification.CredentialVerifier._verify_oauth2', new_callable=AsyncMock) as mock_verify_token:
             mock_verify_token.return_value = (True, "Verified Google Account: test@gmail.com")
             valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
        
        self.assertTrue(valid)
        self.assertIn("Verified Google Account: test@gmail.com", msg)

    @patch('credentials.browser_utils.login_and_extract_tokens')
    def test_verify_website_login_browser(self, mock_browser):
        cred = Credential(user=self.user, credential_type=self.type_custom, name="Web Test")
        cred.get_credential_data = MagicMock(return_value={
            'loginUrl': 'http://test.com', 'username': 'u', 'password': 'p'
        })
        cred.set_credential_data = MagicMock()
        cred.save = MagicMock()
        
        mock_browser.return_value = {'access_token': 'browser-token'}
        
        mock_browser.return_value = {'access_token': 'browser-token'}
        
        # Use MagicMock for sync method
        with patch('credentials.verification.CredentialVerifier._verify_website_login') as mock_verify_web:
             mock_verify_web.return_value = (True, "Login successful: access_token")
             valid, msg = async_to_sync(CredentialVerifier.verify)(cred)
        
        self.assertTrue(valid)
        self.assertIn("Login successful", msg)
        self.assertIn("access_token", msg)


from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

class CredentialsSerializationTests(APITestCase):
    """
    Tests for Credentials serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testadmin', password='password123')
        self.client.force_authenticate(user=self.user)

    def test_oauth_init_validation(self):
        """Test Google OAuth init validation."""
        url = reverse('google-credentials-init')
        
        # Missing redirect_uri
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Let's check the view logic. 
        # Actually, in standard cases it should be required if we want strict validation.
        # If I didn't make it required in the serializer, it will use the default.
        
    def test_oauth_callback_validation(self):
        """Test Google OAuth callback validation."""
        url = reverse('google-credentials-callback')
        
        # Missing code
        data = {'redirect_uri': 'http://localhost:3000/callback'}
        response = self.client.post(url, data, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('code', response.data)


class CredentialPartialUpdateTests(APITestCase):
    """
    The read serializer masks secrets, so a client editing a credential can only
    send back the fields the user retyped. Update must merge, not replace.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='partialuser', password='password123')
        self.client.force_authenticate(user=self.user)
        self.cred_type = CredentialType.objects.create(
            name='Test Service', slug='test-service', auth_method='api_key',
            fields_schema=[
                {'name': 'apiKey', 'label': 'API Key', 'type': 'password', 'required': True},
                {'name': 'baseUrl', 'label': 'Base URL', 'type': 'text',
                 'required': False, 'public': True},
            ],
        )
        self.credential = Credential(
            user=self.user, credential_type=self.cred_type, name='My Cred',
            public_metadata={'baseUrl': 'https://a.example'},
        )
        self.credential.set_credential_data({'apiKey': 'sk-original-secret'})
        self.credential.save()

    def test_untouched_secret_survives_partial_update(self):
        url = reverse('credentials-detail', args=[self.credential.id])
        response = self.client.patch(
            url, {'name': 'Renamed', 'data': {'baseUrl': 'https://b.example'}}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.credential.refresh_from_db()
        self.assertEqual(self.credential.name, 'Renamed')
        self.assertEqual(self.credential.public_metadata['baseUrl'], 'https://b.example')
        self.assertEqual(
            self.credential.get_credential_data()['apiKey'], 'sk-original-secret'
        )

    def test_supplied_secret_overwrites(self):
        url = reverse('credentials-detail', args=[self.credential.id])
        response = self.client.patch(
            url, {'data': {'apiKey': 'sk-new-secret'}}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.credential.refresh_from_db()
        self.assertEqual(self.credential.get_credential_data()['apiKey'], 'sk-new-secret')
        # The public field was not sent, so it is left alone.
        self.assertEqual(self.credential.public_metadata['baseUrl'], 'https://a.example')

    def test_secret_is_masked_on_read(self):
        url = reverse('credentials-detail', args=[self.credential.id])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        api_key_field = next(f for f in response.data['fields'] if f['key'] == 'apiKey')
        self.assertTrue(api_key_field['value'].startswith('********'))
        self.assertNotIn('sk-original-secret', str(response.data))


class CredentialWriteValidationTests(APITestCase):
    """
    The `data` dict is unconstrained by DRF, so the type's `fields_schema`
    must be enforced server-side; and duplicate names must be a 400, not a 500.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='writeuser', password='password123')
        self.client.force_authenticate(user=self.user)
        self.cred_type = CredentialType.objects.create(
            name='Test Service', slug='test-service-write', auth_method='api_key',
            fields_schema=[
                {'name': 'apiKey', 'label': 'API Key', 'type': 'password', 'required': True},
            ],
        )

    def test_create_missing_required_field_is_400(self):
        url = reverse('credentials-list')
        response = self.client.post(
            url,
            {'name': 'No Key', 'credential_type': self.cred_type.id, 'data': {}},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('data', response.data)

    def test_duplicate_name_is_400_not_500(self):
        url = reverse('credentials-list')
        payload = {
            'name': 'Same Name',
            'credential_type': self.cred_type.id,
            'data': {'apiKey': 'sk-one'},
        }
        first = self.client.post(url, payload, format='json')
        self.assertEqual(first.status_code, status.HTTP_201_CREATED)

        second = self.client.post(url, payload, format='json')
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('name', second.data)

    def test_callback_missing_state_is_400(self):
        """A code exchange without the signed state must be refused."""
        url = reverse('google-credentials-callback')
        response = self.client.post(
            url,
            {'code': 'abc123', 'redirect_uri': 'http://localhost:5173/oauth/callback'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('state', response.data.get('error', '').lower())


class CredentialCacheBustingTests(APITestCase):
    """
    The CredentialManager caches decrypted data for 5 minutes; a write through
    the API must not leave that cache serving stale keys (or miss a new row).
    """
    def setUp(self):
        from django.utils import timezone
        self.timezone = timezone
        self.user = User.objects.create_user(username='cacheuser', password='password123')
        self.client.force_authenticate(user=self.user)
        self.cred_type = CredentialType.objects.create(
            name='Cache Service', slug='cache-service', auth_method='api_key',
            fields_schema=[{'name': 'apiKey', 'label': 'API Key', 'type': 'password',
                            'required': True}],
        )
        from credentials.manager import get_credential_manager
        self.mgr = get_credential_manager()
        self.mgr._cache.clear()

    def _prime_cache(self, key):
        self.mgr._cache[key] = ({'apiKey': 'stale'}, self.timezone.now())

    def test_update_busts_cache(self):
        cred = Credential.objects.create(
            user=self.user, credential_type=self.cred_type, name='Cached',
        )
        cred.set_credential_data({'apiKey': 'sk-real'})
        cred.save()
        self._prime_cache(f"{self.user.id}:{cred.id}")

        url = reverse('credentials-detail', args=[cred.id])
        response = self.client.patch(
            url, {'data': {'apiKey': 'sk-new'}}, format='json'
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn(f"{self.user.id}:{cred.id}", self.mgr._cache)

    def test_delete_busts_cache(self):
        cred = Credential.objects.create(
            user=self.user, credential_type=self.cred_type, name='Delete Me',
        )
        cred.set_credential_data({'apiKey': 'sk-real'})
        cred.save()
        self._prime_cache(f"{self.user.id}:{cred.id}")

        url = reverse('credentials-detail', args=[cred.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertNotIn(f"{self.user.id}:{cred.id}", self.mgr._cache)


class CredentialAuditSnapshotTests(APITestCase):
    """
    Deletion history must survive the credential's FK being nulled by
    SET_NULL — the audit row carries a snapshot for exactly that reason.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='audituser', password='password123')
        self.client.force_authenticate(user=self.user)
        self.cred_type = CredentialType.objects.create(
            name='Audit Service', slug='audit-service', auth_method='api_key',
            fields_schema=[{'name': 'apiKey', 'label': 'API Key', 'type': 'password',
                            'required': True}],
        )

    def test_delete_log_keeps_snapshot_after_fk_nulled(self):
        from credentials.models import CredentialAuditLog
        cred = Credential.objects.create(
            user=self.user, credential_type=self.cred_type, name='Ephemeral',
        )
        cred.set_credential_data({'apiKey': 'sk-real'})
        cred.save()

        url = reverse('credentials-detail', args=[cred.id])
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        log = CredentialAuditLog.objects.get(user=self.user, action='deleted')
        self.assertIsNone(log.credential_id)
        self.assertEqual(log.snapshot['name'], 'Ephemeral')
        self.assertEqual(log.snapshot['credential_type'], 'Audit Service')

    def test_audit_log_serializer_falls_back_to_snapshot(self):
        from credentials.models import CredentialAuditLog
        from credentials.serializers import CredentialAuditLogSerializer
        log = CredentialAuditLog.objects.create(
            credential=None, user=self.user, action='deleted',
            snapshot={'name': 'Gone', 'credential_type': 'Audit Service'},
        )
        data = CredentialAuditLogSerializer(log).data
        self.assertEqual(data['credential_name'], 'Gone')
        self.assertEqual(data['credential_type_name'], 'Audit Service')
