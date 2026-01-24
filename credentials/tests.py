from django.test import TestCase
from django.contrib.auth import get_user_model
from unittest.mock import MagicMock, patch
from .verification import CredentialVerifier
from .models import Credential, CredentialType

User = get_user_model()

class CredentialVerifierTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        
        # Create Dummy Types
        self.type_openai = CredentialType.objects.create(
            name='OpenAI', slug='openai', auth_method='api_key',
            fields_schema=[{'name': 'apiKey', 'required': True}]
        )
        self.type_slack = CredentialType.objects.create(
            name='Slack', slug='slack', auth_method='bearer',
            fields_schema=[{'name': 'token', 'required': True}]
        )
        self.type_google = CredentialType.objects.create(
            name='Google', slug='google-oauth2', auth_method='oauth2',
            oauth_config={'auth_url': 'https://accounts.google.com', 'token_url': 'https://oauth2.googleapis.com/token'}
        )
        self.type_custom = CredentialType.objects.create(
            name='Website', slug='website-login', auth_method='custom',
            fields_schema=[{'name': 'loginUrl', 'required': True}]
        )

    @patch('requests.get')
    def test_verify_api_key_openai_success(self, mock_get):
        cred = Credential(user=self.user, credential_type=self.type_openai, name="Test OpenAI")
        # Mocking get_credential_data returning decrypted dict
        cred.get_credential_data = MagicMock(return_value={'apiKey': 'sk-123'})
        
        mock_get.return_value.status_code = 200
        
        valid, msg = CredentialVerifier.verify(cred)
        self.assertTrue(valid)
        self.assertIn("Successfully connected", msg)

    @patch('requests.get')
    def test_verify_api_key_openai_failure(self, mock_get):
        cred = Credential(user=self.user, credential_type=self.type_openai, name="Fail OpenAI")
        cred.get_credential_data = MagicMock(return_value={'apiKey': 'sk-bad'})
        
        mock_get.return_value.status_code = 401
        valid, msg = CredentialVerifier.verify(cred)
        self.assertFalse(valid)
        self.assertIn("Invalid API Key", msg)

    @patch('requests.post')
    def test_verify_bearer_slack(self, mock_post):
        cred = Credential(user=self.user, credential_type=self.type_slack, name="Slack Test")
        cred.get_credential_data = MagicMock(return_value={'token': 'xoxb-123'})
        
        mock_post.return_value.status_code = 200
        mock_post.return_value.json.return_value = {'ok': True, 'user': 'bot', 'team': 'AIAAS'}
        
        valid, msg = CredentialVerifier.verify(cred)
        self.assertTrue(valid)
        self.assertIn("Connected as bot", msg)

    def test_verify_oauth2_missing_config(self):
        # Create type with missing config
        type_bad = CredentialType.objects.create(name='Bad OAuth', slug='bad', auth_method='oauth2')
        cred = Credential(user=self.user, credential_type=type_bad, name="Bad OAuth")
        cred.get_credential_data = MagicMock(return_value={})
        
        valid, msg = CredentialVerifier.verify(cred)
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
        
        valid, msg = CredentialVerifier.verify(cred)
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
        
        valid, msg = CredentialVerifier.verify(cred)
        self.assertTrue(valid)
        self.assertIn("Login successful", msg)
        self.assertIn("access_token", msg)
