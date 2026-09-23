"""
Sharing a custom tool, and sharing an agent that uses one.

The cases worth having are the silent ones. A snapshot that carries a
secret does not error — it hands the author's credential to a stranger, and
nothing downstream can detect that it did. An agent installed without its
tool does not error either — it answers from nothing and looks merely
stupid. So most of what is tested below is what must *not* travel, and what
an install must have created.
"""
from __future__ import annotations

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from agents.models import SubAgent
from agents.views.agents import AgentSerializer
from credentials.models import CredentialType
from datasources.models import ApiConnection, DataConnection, SharedTool

User = get_user_model()

PUBLIC_URL = 'https://93.184.216.34/v1'


def _api(user, **overrides):
    body = {'name': 'Acme', 'base_url': PUBLIC_URL,
            'auth': {'type': 'bearer', 'secret_ref': 'test-api.api_key'}}
    body.update(overrides)
    return ApiConnection.objects.create(user=user, **body)


class ToolSharingTests(TestCase):
    def setUp(self):
        self.author = User.objects.create_user(username='author', password='x')
        self.viewer = User.objects.create_user(username='viewer', password='x')
        CredentialType.objects.create(name='Test API', slug='test-api')
        self.row = _api(self.author)
        self.aclient = APIClient()
        self.aclient.force_authenticate(self.author)
        self.vclient = APIClient()
        self.vclient.force_authenticate(self.viewer)

    def _share(self, **body):
        url = f'/api/datasources/api/{self.row.id}/share/'
        payload = {'tagline': 'Acme orders API', **body}
        return self.aclient.post(url, payload, format='json')

    def test_no_secret_survives_the_snapshot(self):
        res = self._share()
        self.assertEqual(res.status_code, 200, res.content)
        share = SharedTool.objects.get(slug=res.data['slug'])
        dumped = json.dumps({'c': share.tool_config, 'a': share.auth_shape})
        # No reference and no value — only the credential *type* slug, which
        # is public vocabulary telling the installer what to link.
        self.assertNotIn('secret_ref', dumped)
        self.assertEqual(share.auth_shape['needs'],
                         {'slug': 'test-api', 'field': 'api_key'})

    def test_preview_shows_what_would_travel(self):
        url = f'/api/datasources/api/{self.row.id}/share/'
        res = self.aclient.get(url)
        self.assertEqual(res.status_code, 200)
        self.assertFalse(res.data['published'])
        self.assertEqual(res.data['auth_shape']['needs'],
                         {'slug': 'test-api', 'field': 'api_key'})

    def test_publish_requires_a_tagline(self):
        url = f'/api/datasources/api/{self.row.id}/share/'
        res = self.aclient.post(url, {}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_install_creates_an_unauthenticated_private_copy(self):
        slug = self._share().data['slug']
        res = self.vclient.post(f'/api/datasources/shared/{slug}/install/')
        self.assertEqual(res.status_code, 201, res.content)
        copy = ApiConnection.objects.get(user=self.viewer)
        self.assertEqual(copy.base_url, PUBLIC_URL)
        # Credentials never travel — not as values, not even as references.
        self.assertEqual(copy.auth, {'type': 'none'})
        self.assertEqual(res.data['credentials_needed'],
                         [{'tool': copy.name, 'slug': 'test-api',
                           'field': 'api_key'}])
        share = SharedTool.objects.get(slug=slug)
        self.assertEqual(share.install_count, 1)

    def test_installing_twice_does_not_collide(self):
        slug = self._share().data['slug']
        url = f'/api/datasources/shared/{slug}/install/'
        self.assertEqual(self.vclient.post(url).status_code, 201)
        self.assertEqual(self.vclient.post(url).status_code, 201)
        self.assertEqual(ApiConnection.objects.filter(user=self.viewer).count(), 2)

    def test_link_share_is_invisible_in_listings_but_installable(self):
        slug = self._share(visibility='link').data['slug']
        res = self.vclient.get('/api/datasources/shared/')
        self.assertNotIn(slug, [r['slug'] for r in res.data['results']])
        res = self.vclient.get(f'/api/datasources/shared/{slug}/')
        self.assertEqual(res.status_code, 200)
        res = self.vclient.post(f'/api/datasources/shared/{slug}/install/')
        self.assertEqual(res.status_code, 201)

    def test_withdrawn_share_is_gone_for_others(self):
        slug = self._share().data['slug']
        url = f'/api/datasources/api/{self.row.id}/share/'
        self.assertEqual(self.aclient.delete(url).status_code, 204)
        res = self.vclient.get(f'/api/datasources/shared/{slug}/')
        self.assertEqual(res.status_code, 404)
        res = self.vclient.post(f'/api/datasources/shared/{slug}/install/')
        self.assertEqual(res.status_code, 404)
        # …but the author can still see and relist it.
        res = self.aclient.get(f'/api/datasources/shared/{slug}/')
        self.assertEqual(res.status_code, 200)

    def test_foreign_tool_cannot_be_published(self):
        url = f'/api/datasources/api/{self.row.id}/share/'
        res = self.vclient.post(url, {'tagline': 'Mine now'}, format='json')
        self.assertEqual(res.status_code, 404)

    def test_deleting_the_source_leaves_the_listing_installable(self):
        slug = self._share().data['slug']
        self.row.delete()
        res = self.vclient.post(f'/api/datasources/shared/{slug}/install/')
        self.assertEqual(res.status_code, 201)


class AgentCarriesItsToolsTests(TestCase):
    """Sharing an agent shares its custom tools automatically."""

    def setUp(self):
        self.author = User.objects.create_user(username='author', password='x')
        self.viewer = User.objects.create_user(username='viewer', password='x')
        CredentialType.objects.create(name='Test API', slug='test-api')
        self.tool = _api(self.author)
        request = type('R', (), {'user': self.author})()
        data = {
            'name': 'Order checker',
            'brief': 'Check orders.',
            'tools': {'api': True},
            'apiConnections': [self.tool.id],
            'autonomy': 'ask',
        }
        serializer = AgentSerializer(data=data, context={'request': request})
        assert serializer.is_valid(), serializer.errors
        self.agent = AgentSerializer.apply(SubAgent(user=self.author),
                                           serializer.validated_data)
        self.agent.save()
        self.aclient = APIClient()
        self.aclient.force_authenticate(self.author)
        self.vclient = APIClient()
        self.vclient.force_authenticate(self.viewer)

    def _publish(self):
        url = reverse('orchestrator:agent_share', args=[self.agent.id])
        res = self.aclient.post(url, {'tagline': 'Checks orders.'},
                                format='json')
        assert res.status_code == 200, res.content
        return res.data['slug']

    def test_tool_id_becomes_a_requirement_with_a_snapshot(self):
        from agents import publishing

        _, requirements = publishing.to_shareable(self.agent)
        req = next(r for r in requirements if r['type'] == 'api_tool')
        self.assertEqual(req['snapshot']['config']['base_url'], PUBLIC_URL)
        dumped = json.dumps(req['snapshot'])
        self.assertNotIn('secret_ref', dumped)

    def test_install_takes_the_snapshot_as_a_private_copy(self):
        slug = self._publish()
        url = reverse('orchestrator:template_install', args=[slug])
        res = self.vclient.post(url, {'requirements': {'api_tool_1': 'install'}},
                                format='json')
        self.assertEqual(res.status_code, 201, res.content)
        copy = ApiConnection.objects.get(user=self.viewer)
        self.assertEqual(copy.auth, {'type': 'none'})
        installed = SubAgent.objects.get(id=res.data['id'])
        self.assertEqual(installed.agent_context['apiConnections'], [copy.id])
        self.assertEqual(res.data['credentials_needed'],
                         [{'tool': copy.name, 'slug': 'test-api',
                           'field': 'api_key'}])

    def test_install_may_point_at_an_existing_connection_instead(self):
        slug = self._publish()
        mine = _api(self.viewer, name='Mine')
        url = reverse('orchestrator:template_install', args=[slug])
        res = self.vclient.post(url, {'requirements': {'api_tool_1': mine.id}},
                                format='json')
        self.assertEqual(res.status_code, 201, res.content)
        installed = SubAgent.objects.get(id=res.data['id'])
        self.assertEqual(installed.agent_context['apiConnections'], [mine.id])
        self.assertFalse(ApiConnection.objects.filter(
            user=self.viewer).exclude(id=mine.id).exists())

    def test_installing_someone_elses_connection_is_refused(self):
        slug = self._publish()
        url = reverse('orchestrator:template_install', args=[slug])
        res = self.vclient.post(
            url, {'requirements': {'api_tool_1': self.tool.id}}, format='json')
        self.assertEqual(res.status_code, 400)

    def test_a_vanished_tool_fails_the_publish(self):
        from agents import publishing

        self.tool.delete()
        with self.assertRaises(publishing.PublishError):
            publishing.to_shareable(self.agent)

    def test_data_tool_travels_too(self):
        db = DataConnection.objects.create(
            user=self.author, kind='postgres', name='Warehouse',
            host='db.example.com', database='analytics')
        self.agent.agent_context['dataConnections'] = [db.id]
        self.agent.save()
        from agents import publishing

        _, requirements = publishing.to_shareable(self.agent)
        kinds = sorted(r['type'] for r in requirements)
        self.assertEqual(kinds, ['api_tool', 'data_tool'])
