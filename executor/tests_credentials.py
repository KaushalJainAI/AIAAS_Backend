from django.test import TestCase
from django.contrib.auth import get_user_model
from credentials.models import Credential, CredentialType
from executor.credential_utils import get_user_credentials

User = get_user_model()

class CredentialIntegrationTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='testuser', password='password')
        self.cred_type = CredentialType.objects.create(
            name='Test Service',
            slug='test-service',
            service_identifier='test_service_node', # Maps to nodeType
            auth_method='api_key'
        )
        
    def test_get_user_credentials(self):
        # Create a credential
        cred = Credential.objects.create(
            user=self.user,
            credential_type=self.cred_type,
            name='My Test Credential',
            public_metadata={'username': 'user'},
        )
        cred.set_credential_data({'api_key': 'secret_value'})
        cred.save()
        
        # Test utility
        creds_map = get_user_credentials(self.user.id)
        
        # Check mapping by ID
        self.assertIn(str(cred.id), creds_map)
        self.assertEqual(creds_map[str(cred.id)]['api_key'], 'secret_value')
        
        # Check mapping by service_identifier
        self.assertIn('test_service_node', creds_map)
        self.assertEqual(creds_map['test_service_node']['api_key'], 'secret_value')
        self.assertEqual(creds_map['test_service_node']['_source_cred_id'], cred.id)

    def test_multiple_credentials_priority(self):
        # Create two credentials for same service
        # 1. Unverified
        c1 = Credential.objects.create(
             user=self.user,
             credential_type=self.cred_type,
             name='C1',
             is_verified=False
        )
        c1.set_credential_data({'key': 'val1'})
        c1.save()
        
        # 2. Verified
        c2 = Credential.objects.create(
             user=self.user,
             credential_type=self.cred_type,
             name='C2',
             is_verified=True
        )
        c2.set_credential_data({'key': 'val2'})
        c2.save()
        
        creds_map = get_user_credentials(self.user.id)
        
        # Should pick the verified one
        self.assertEqual(creds_map['test_service_node']['key'], 'val2')
        self.assertEqual(creds_map['test_service_node']['_source_cred_id'], c2.id)
