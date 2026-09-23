"""
Private CRUD for custom tools (datasources).

Pinned: a connection row belongs to exactly one user. Every refusal for a
foreign row is the same 404 a missing row gives — a 403 for "exists but not
yours" would be an ownership oracle. Validation fails at write (bad URL,
SSRF host, unknown credential-type slug, duplicate name), never silently at
call time. Raw secrets are never accepted — `auth` carries a reference or,
for anonymous APIs, nothing.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from credentials.models import CredentialType
from datasources.models import ApiConnection, DataConnection

User = get_user_model()

#: A literal public IP: passes the SSRF guard with no DNS round trip, so the
#: success-path tests work offline. (A hostname would resolve via DNS.)
PUBLIC_URL = 'https://93.184.216.34/v1'


class CustomToolsBaseTest(TestCase):
    def setUp(self):
        self.alice = User.objects.create_user(username='alice', password='x')
        self.bob = User.objects.create_user(username='bob', password='x')
        self.client = APIClient()
        self.client.force_authenticate(self.alice)
        CredentialType.objects.create(name='Test API', slug='test-api')

    def _api(self, **overrides):
        body = {'name': 'Acme', 'base_url': PUBLIC_URL}
        body.update(overrides)
        return self.client.post('/api/datasources/api-connections/', body,
                                format='json')

    def _db(self, **overrides):
        body = {'name': 'Warehouse', 'kind': 'postgres',
                'host': 'db.example.com', 'database': 'analytics'}
        body.update(overrides)
        return self.client.post('/api/datasources/data-connections/', body,
                                format='json')


class AuthTests(CustomToolsBaseTest):
    def test_unauthenticated_is_refused(self):
        anon = APIClient()
        res = anon.get('/api/datasources/api-connections/')
        self.assertIn(res.status_code, (401, 403))
        res = anon.get('/api/datasources/data-connections/')
        self.assertIn(res.status_code, (401, 403))


class ApiConnectionTests(CustomToolsBaseTest):
    def test_create_anonymous(self):
        res = self._api()
        self.assertEqual(res.status_code, 201, res.content)
        row = ApiConnection.objects.get(user=self.alice)
        self.assertEqual(row.auth, {'type': 'none'})

    def test_create_with_vault_reference(self):
        res = self._api(auth={'type': 'bearer',
                              'secret_ref': 'test-api.api_key'})
        self.assertEqual(res.status_code, 201, res.content)
        row = ApiConnection.objects.get(user=self.alice)
        self.assertEqual(row.auth['secret_ref'], 'test-api.api_key')

    def test_auth_without_reference_is_refused(self):
        res = self._api(auth={'type': 'bearer'})
        self.assertEqual(res.status_code, 400)

    def test_unknown_credential_type_is_refused(self):
        res = self._api(auth={'type': 'bearer',
                              'secret_ref': 'nope.api_key'})
        self.assertEqual(res.status_code, 400)

    def test_malformed_reference_is_refused(self):
        res = self._api(auth={'type': 'bearer', 'secret_ref': 'not a ref'})
        self.assertEqual(res.status_code, 400)

    def test_non_http_url_is_refused(self):
        res = self._api(base_url='ftp://files.example.com/x')
        self.assertEqual(res.status_code, 400)

    def test_private_host_is_refused(self):
        for bad in ('http://127.0.0.1:8000/x',
                    'https://10.0.0.5/api',
                    'http://169.254.169.254/latest/'):
            res = self._api(name=bad, base_url=bad)
            self.assertEqual(res.status_code, 400, bad)

    def test_unknown_method_is_refused(self):
        res = self._api(allowed_methods=['GET', 'FROBNICATE'])
        self.assertEqual(res.status_code, 400)

    def test_duplicate_name_is_refused(self):
        self.assertEqual(self._api().status_code, 201)
        res = self._api()
        self.assertEqual(res.status_code, 400)

    def test_same_name_for_another_user_is_fine(self):
        self.assertEqual(self._api().status_code, 201)
        bob = APIClient()
        bob.force_authenticate(self.bob)
        res = bob.post('/api/datasources/api-connections/',
                       {'name': 'Acme', 'base_url': PUBLIC_URL}, format='json')
        self.assertEqual(res.status_code, 201, res.content)

    def test_openapi_spec_too_large_is_refused(self):
        big = {'paths': {f'/p{i}': {'get': {}} for i in range(40000)}}
        res = self._api(openapi_spec=big)
        self.assertEqual(res.status_code, 400)

    def test_operations_count_is_reported(self):
        spec = {'paths': {'/orders': {'get': {'summary': 'List orders'}},
                          '/orders/{id}': {'delete': {}}}}
        res = self._api(openapi_spec=spec)
        self.assertEqual(res.status_code, 201, res.content)
        self.assertEqual(res.data['operations_count'], 2)


class DataConnectionTests(CustomToolsBaseTest):
    def test_create_postgres(self):
        res = self._db()
        self.assertEqual(res.status_code, 201, res.content)

    def test_sqlite_needs_a_workspace_path(self):
        res = self._db(kind='sqlite', host='', vfs_path='')
        self.assertEqual(res.status_code, 400)
        res = self._db(kind='sqlite', host='', vfs_path='/Chat/sales.db')
        self.assertEqual(res.status_code, 201, res.content)

    def test_missing_host_is_refused(self):
        res = self._db(host='')
        self.assertEqual(res.status_code, 400)

    def test_private_host_is_refused(self):
        res = self._db(host='192.168.1.10')
        self.assertEqual(res.status_code, 400)

    def test_bad_port_is_refused(self):
        res = self._db(port=99999)
        self.assertEqual(res.status_code, 400)

    def test_secret_ref_validated_like_api_auth(self):
        res = self._db(secret_ref='nope.password')
        self.assertEqual(res.status_code, 400)
        res = self._db(secret_ref='test-api.password')
        self.assertEqual(res.status_code, 201, res.content)


class IsolationTests(CustomToolsBaseTest):
    """Another user's rows are 404 everywhere — never 403, never listed."""

    def setUp(self):
        super().setUp()
        res = self._api()
        assert res.status_code == 201, res.content
        self.row_id = res.data['id']
        res = self._db()
        assert res.status_code == 201, res.content
        self.db_id = res.data['id']
        self.bob_client = APIClient()
        self.bob_client.force_authenticate(self.bob)

    def test_list_shows_only_own_rows(self):
        res = self.bob_client.get('/api/datasources/api-connections/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 0)
        res = self.bob_client.get('/api/datasources/data-connections/')
        self.assertEqual(res.data['count'], 0)

    def test_foreign_rows_are_404(self):
        urls = [
            f'/api/datasources/api-connections/{self.row_id}/',
            f'/api/datasources/data-connections/{self.db_id}/',
        ]
        for url in urls:
            self.assertEqual(self.bob_client.get(url).status_code, 404, url)
            self.assertEqual(
                self.bob_client.patch(url, {'name': 'Hijacked'},
                                      format='json').status_code, 404, url)
            self.assertEqual(
                self.bob_client.delete(url).status_code, 404, url)

    def test_owner_can_update_and_delete(self):
        url = f'/api/datasources/api-connections/{self.row_id}/'
        res = self.client.patch(url, {'name': 'Acme v2'}, format='json')
        self.assertEqual(res.status_code, 200)
        res = self.client.delete(url)
        self.assertEqual(res.status_code, 204)
        self.assertFalse(
            ApiConnection.objects.filter(pk=self.row_id).exists())

    def test_rename_to_own_existing_name_is_refused(self):
        self.assertEqual(self._api(name='Second').status_code, 201)
        url = f'/api/datasources/api-connections/{self.row_id}/'
        res = self.client.patch(url, {'name': 'Second'}, format='json')
        self.assertEqual(res.status_code, 400)
