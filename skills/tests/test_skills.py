from unittest.mock import patch
from types import SimpleNamespace

from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase
from django.contrib.auth.models import User

from skills.models import Skill

class SkillsSerializationTests(APITestCase):
    """
    Tests for Skills serializers and views validation.
    """
    def setUp(self):
        self.user = User.objects.create_user(username='testcreator', password='password123')
        self.client.force_authenticate(user=self.user)

    def test_skill_search_validation(self):
        """Test skill search input validation."""
        url = reverse('skill-search')
        
        # Invalid tab (not 'mine' or 'public')
        response = self.client.get(url, {'tab': 'invalid'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('tab', response.data)

        # Invalid page (not an integer)
        response = self.client.get(url, {'page': 'first'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('page', response.data)

        # Valid search
        response = self.client.get(url, {'query': 'test', 'tab': 'public'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn('results', response.data)

    def test_search_survives_an_unavailable_embedder(self):
        """A query must still rank on fuzzy match when embedding fails.

        The case above passes even unguarded, because `hybrid_search` returns
        early when the user owns nothing and never reaches the embedder. Seed a
        row first so the query actually gets that far.
        """
        Skill.objects.create(
            user=self.user,
            title='SQL Query Optimization',
            description='Diagnose a slow query from its plan.',
            content='# SQL Query Optimization',
            category='Development',
        )

        with patch(
            'skills.services.get_skills_knowledge_base',
            side_effect=RuntimeError('no embedder configured'),
        ):
            response = self.client.get(reverse('skill-search'), {'query': 'slow sql', 'tab': 'mine'})

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['total'], 1)
        self.assertEqual(response.data['results'][0]['title'], 'SQL Query Optimization')

    def test_search_ranks_by_kb_index_when_available(self):
        """A working index decides the order; ORM scoping is re-applied on top."""
        own = Skill.objects.create(
            user=self.user, title='Ranked Skill', content='vector content',
            category='Development',
        )
        other = Skill.objects.create(
            user=self.user, title='Runner-up', content='other content',
            category='Development',
        )

        class FakeKb:
            async def search(self, query, top_k):
                # The index "thinks" the second skill is the better match.
                return [SimpleNamespace(metadata={'skill_id': other.id}),
                        SimpleNamespace(metadata={'skill_id': own.id})]

        with patch('skills.services.get_skills_knowledge_base', return_value=FakeKb()):
            response = self.client.get(
                reverse('skill-search'), {'query': 'vector', 'tab': 'mine'}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        titles = [r['title'] for r in response.data['results']]
        self.assertEqual(titles, ['Runner-up', 'Ranked Skill'])

    def test_search_ignores_stale_index_entries(self):
        """An index hit for a deleted skill never appears in results."""
        Skill.objects.create(
            user=self.user, title='Living Skill', content='alive',
            category='Development',
        )

        class FakeKb:
            async def search(self, query, top_k):
                return [SimpleNamespace(metadata={'skill_id': 99999})]

        with patch('skills.services.get_skills_knowledge_base', return_value=FakeKb()):
            response = self.client.get(
                reverse('skill-search'), {'query': 'anything', 'tab': 'mine'}
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['results'], [])
        self.assertEqual(response.data['total'], 0)


class SkillAuthorizationTests(APITestCase):
    def setUp(self):
        self.owner = User.objects.create_user(username='owner', password='password123')
        self.other = User.objects.create_user(username='other', password='password123')
        self.admin = User.objects.create_superuser(username='admin', password='password123')
        self.skill = Skill.objects.create(
            user=self.owner,
            title='Shared Skill',
            description='Publicly readable.',
            content='Read me, but do not edit me.',
            is_shared=True,
        )

    def test_non_owner_can_read_shared_skill(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.get(reverse('skill-detail', args=[self.skill.pk]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_owner_cannot_update_shared_skill(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.patch(
            reverse('skill-detail', args=[self.skill.pk]),
            {'title': 'Hijacked'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.skill.refresh_from_db()
        self.assertEqual(self.skill.title, 'Shared Skill')

    def test_non_owner_cannot_delete_shared_skill(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.delete(reverse('skill-detail', args=[self.skill.pk]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(Skill.objects.filter(pk=self.skill.pk).exists())

    def test_non_owner_cannot_toggle_sharing(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.post(reverse('skill-share', args=[self.skill.pk]))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_owner_can_update_own_skill(self):
        self.client.force_authenticate(user=self.owner)
        response = self.client.patch(
            reverse('skill-detail', args=[self.skill.pk]),
            {'title': 'Renamed'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_admin_can_update_others_skill(self):
        self.client.force_authenticate(user=self.admin)
        response = self.client.patch(
            reverse('skill-detail', args=[self.skill.pk]),
            {'title': 'Admin-edited'},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_owner_can_fork_shared_skill(self):
        self.client.force_authenticate(user=self.other)
        response = self.client.post(reverse('skill-fork', args=[self.skill.pk]))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        forked = Skill.objects.get(pk=response.data['id'])
        self.assertEqual(forked.user, self.other)
        self.assertEqual(forked.author_name, 'owner')


class SkillContentLimitTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='writer', password='password123')
        self.client.force_authenticate(user=self.user)

    def _payload(self, content):
        return {'title': 'Wordy Skill', 'description': '', 'content': content}

    def test_content_over_word_limit_is_rejected(self):
        content = 'word ' * 10_001
        response = self.client.post(reverse('skill-list'), self._payload(content), format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('content', response.data)

    def test_content_at_word_limit_is_accepted(self):
        content = 'word ' * 10_000
        response = self.client.post(reverse('skill-list'), self._payload(content), format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_update_cannot_exceed_word_limit(self):
        skill = Skill.objects.create(
            user=self.user, title='Short', content='fine',
        )
        response = self.client.patch(
            reverse('skill-detail', args=[skill.pk]),
            {'content': 'word ' * 10_001},
            format='json',
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
