"""
Publishing an agent for other people to install.

The cases worth having are the ones where the mistake is invisible. Publishing
is the moment a private configuration full of the author's row ids becomes
something a stranger installs, and every failure here is silent by nature: an
id that travels does not error, it reads *somebody else's* row; a field added
to `AgentConfig` next month is published by default unless the projection is an
allow-list; a snapshot that is really a pointer lets an author widen the grants
of something already listed without anyone re-consenting. So most of what is
tested below is what must *not* be in the payload.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import KnowledgeBase
from mcp_integration.models import MCPServer
from skills.models import Skill

from agents import publishing
from agents.models import SharedAgent, SubAgent
from agents.views.agents import AgentSerializer


def curated(name):
    return MCPServer.objects.get(name=name, user__isnull=True).id


class PublishingProjectionTests(APITestCase):
    """`to_shareable` — what leaves an account, and what cannot."""

    def setUp(self):
        self.user = User.objects.create_user('author', 'a@example.com', 'pw')
        self.kb = KnowledgeBase.objects.create(user=self.user, name='Vendor records')
        self.skill = Skill.objects.create(
            user=self.user, title='GSTIN rules', content='...'
        )
        self.agent = self._agent()

    def _agent(self, **overrides):
        request = type('R', (), {'user': self.user})()
        data = {
            'name': 'Reconciler',
            'brief': 'Reconcile invoices.',
            'tools': {'rag': True, 'mcp': True},
            'knowledgeBases': [self.kb.id],
            'skills': [self.skill.id],
            'connectors': [curated('Gmail')],
            'autonomy': 'ask',
            **overrides,
        }
        serializer = AgentSerializer(data=data, context={'request': request})
        assert serializer.is_valid(), serializer.errors
        agent = AgentSerializer.apply(SubAgent(user=self.user),
                                      serializer.validated_data)
        agent.save()
        return agent

    def test_no_row_id_survives_the_projection(self):
        config, requirements = publishing.to_shareable(self.agent)
        for field in ('connectors', 'knowledgeBases', 'skills'):
            self.assertNotIn(field, config)
        self.assertEqual(len(requirements), 3)

    def test_every_id_becomes_a_requirement_rather_than_being_dropped(self):
        """Dropping is how an agent arrives missing the corpus it was built on."""
        _, requirements = publishing.to_shareable(self.agent)
        kinds = sorted(r['type'] for r in requirements)
        self.assertEqual(kinds, ['connector', 'knowledge_base', 'skill'])

    def test_a_connector_requirement_carries_the_icon_slug_as_its_hint(self):
        _, requirements = publishing.to_shareable(self.agent)
        connector = next(r for r in requirements if r['type'] == 'connector')
        self.assertEqual(connector['provider'], 'gmail')

    def test_the_projection_is_an_allow_list_not_a_denylist(self):
        """A field added to AgentConfig later must not publish itself.

        This is the test that fails when someone adds a knob carrying something
        private: the new key is simply absent until it is added to
        `SHAREABLE_KEYS` deliberately.
        """
        config, _ = publishing.to_shareable(self.agent)
        self.assertTrue(set(config).issubset(publishing.SHAREABLE_KEYS))

    def test_facts_about_the_authors_copy_do_not_travel(self):
        config, _ = publishing.to_shareable(self.agent)
        for key in ('id', 'status', 'created_at', 'updated_at', 'runs',
                    'spend', 'unattended', 'extraSchedules',
                    'scheduleTimezone'):
            self.assertNotIn(key, config)

    def test_a_vanished_knowledge_base_fails_the_publish(self):
        """The author is present and can fix it; the installer would not be."""
        self.kb.delete()
        with self.assertRaises(publishing.PublishError):
            publishing.to_shareable(self.agent)

    def test_the_published_config_is_installable(self):
        """A projection the serializer refuses is a listing nobody can use."""
        config, requirements = publishing.to_shareable(self.agent)
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        kb = KnowledgeBase.objects.create(user=other, name='Theirs')
        skill = Skill.objects.create(user=other, title='Theirs', content='.')
        request = type('R', (), {'user': other})()
        resolved = {
            **config,
            'knowledgeBases': [kb.id],
            'skills': [skill.id],
            'connectors': [curated('Gmail')],
        }
        serializer = AgentSerializer(data=resolved, context={'request': request})
        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_an_author_may_reword_a_requirement_but_not_retype_it(self):
        _, generated = publishing.to_shareable(self.agent)
        target = generated[0]
        edited = publishing.sanitise_requirements(generated, [{
            'key': target['key'], 'label': 'Your mailbox',
            'type': 'knowledge_base', 'why': 'Because.',
        }])
        changed = next(r for r in edited if r['key'] == target['key'])
        self.assertEqual(changed['label'], 'Your mailbox')
        self.assertEqual(changed['why'], 'Because.')
        self.assertEqual(changed['type'], target['type'])


class ShareEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('author', 'a@example.com', 'pw')
        self.client.force_authenticate(user=self.user)
        self.agent = SubAgent.objects.create(
            user=self.user, name='Reconciler', prompt='Reconcile invoices.',
            tool_grants={'webSearch': True}, tags=['finance'],
        )
        self.url = reverse('orchestrator:agent_share', args=[self.agent.id])

    def test_preview_writes_nothing(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data['published'])
        self.assertFalse(SharedAgent.objects.exists())

    def test_publishing_requires_a_tagline(self):
        response = self.client.post(self.url, {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SharedAgent.objects.exists())

    def test_publishing_creates_a_listing(self):
        response = self.client.post(
            self.url, {'tagline': 'Reconciles invoices.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share = SharedAgent.objects.get()
        self.assertEqual(share.author, self.user)
        self.assertEqual(share.slug, 'reconciler')
        self.assertEqual(share.version, 1)

    def test_republishing_keeps_the_slug_and_bumps_the_version(self):
        """The link people already have must not rot because of a rename."""
        self.client.post(self.url, {'tagline': 'One.'}, format='json')
        self.agent.name = 'Invoice reconciler'
        self.agent.save()
        response = self.client.post(self.url, {'tagline': 'Two.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        share = SharedAgent.objects.get()
        self.assertEqual(share.slug, 'reconciler')
        self.assertEqual(share.version, 2)
        self.assertEqual(share.tagline, 'Two.')

    def test_a_slug_never_collides_with_a_curated_template(self):
        """A shared agent taking a curated slug would be unreachable for ever."""
        agent = SubAgent.objects.create(user=self.user, name='Deep research')
        response = self.client.post(
            reverse('orchestrator:agent_share', args=[agent.id]),
            {'tagline': 'Mine.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotEqual(response.data['slug'], 'deep-research')

    def test_two_authors_can_publish_the_same_name(self):
        self.client.post(self.url, {'tagline': 'Mine.'}, format='json')
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        theirs = SubAgent.objects.create(user=other, name='Reconciler')
        self.client.force_authenticate(user=other)
        response = self.client.post(
            reverse('orchestrator:agent_share', args=[theirs.id]),
            {'tagline': 'Theirs.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(SharedAgent.objects.count(), 2)
        self.assertNotEqual(response.data['slug'], 'reconciler')

    def test_you_cannot_publish_somebody_elses_agent(self):
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        theirs = SubAgent.objects.create(user=other, name='Theirs')
        response = self.client.post(
            reverse('orchestrator:agent_share', args=[theirs.id]),
            {'tagline': 'Not mine.'}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_withdrawing_unlists_rather_than_deletes(self):
        """An install already made keeps working; relisting keeps the URL."""
        self.client.post(self.url, {'tagline': 'Mine.'}, format='json')
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        share = SharedAgent.objects.get()
        self.assertFalse(share.is_listed)

    def test_the_author_is_credited_by_name_never_by_email(self):
        self.client.post(self.url, {'tagline': 'Mine.'}, format='json')
        response = self.client.get(reverse('orchestrator:template_list'))
        mine = next(t for t in response.data if t['slug'] == 'reconciler')
        self.assertEqual(mine['author'], 'author')
        self.assertNotIn('@', mine['author'])

    def test_deleting_the_source_agent_leaves_the_listing_installable(self):
        self.client.post(self.url, {'tagline': 'Mine.'}, format='json')
        self.agent.delete()
        share = SharedAgent.objects.get()
        self.assertIsNone(share.subagent_id)
        response = self.client.get(
            reverse('orchestrator:template_detail', args=[share.slug]))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_a_listing_is_a_snapshot_not_a_live_view(self):
        """Editing the agent must not change what strangers already approved."""
        self.client.post(self.url, {'tagline': 'Mine.'}, format='json')
        self.agent.tool_grants = {'shell': True}
        self.agent.save()
        share = SharedAgent.objects.get()
        self.assertFalse(share.config['tools'].get('shell'))


class ExploreTests(APITestCase):
    """What each viewer may see, and install."""

    def setUp(self):
        self.author = User.objects.create_user('author', 'a@example.com', 'pw')
        self.viewer = User.objects.create_user('viewer', 'v@example.com', 'pw')
        self.agent = SubAgent.objects.create(
            user=self.author, name='Reconciler', prompt='Reconcile.',
            tool_grants={'webSearch': True},
        )
        self.client.force_authenticate(user=self.author)
        self.share_url = reverse('orchestrator:agent_share', args=[self.agent.id])

    def publish(self, **body):
        return self.client.post(
            self.share_url, {'tagline': 'Reconciles invoices.', **body},
            format='json')

    def test_a_platform_share_is_listed_to_everyone(self):
        self.publish(visibility='platform')
        self.client.force_authenticate(user=self.viewer)
        response = self.client.get(reverse('orchestrator:template_list'))
        slugs = {t['slug'] for t in response.data}
        self.assertIn('reconciler', slugs)

    def test_a_link_share_is_not_listed_but_is_reachable(self):
        """That difference is the whole point of having two visibilities."""
        self.publish(visibility='link')
        self.client.force_authenticate(user=self.viewer)

        listing = self.client.get(reverse('orchestrator:template_list'))
        self.assertNotIn('reconciler', {t['slug'] for t in listing.data})

        detail = self.client.get(
            reverse('orchestrator:template_detail', args=['reconciler']))
        self.assertEqual(detail.status_code, status.HTTP_200_OK)

    def test_a_withdrawn_share_is_gone_for_everyone_but_its_author(self):
        self.publish()
        self.client.delete(self.share_url)

        self.client.force_authenticate(user=self.viewer)
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['reconciler']))
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.client.force_authenticate(user=self.author)
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['reconciler']))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_the_listing_carries_both_sources(self):
        self.publish()
        response = self.client.get(reverse('orchestrator:template_list'))
        sources = {t['source'] for t in response.data}
        self.assertEqual(sources, {'curated', 'community'})

    def test_source_filter_narrows_the_listing(self):
        self.publish()
        community = self.client.get(
            reverse('orchestrator:template_list'), {'source': 'community'})
        self.assertEqual({t['source'] for t in community.data}, {'community'})
        curated_only = self.client.get(
            reverse('orchestrator:template_list'), {'source': 'curated'})
        self.assertEqual({t['source'] for t in curated_only.data}, {'curated'})

    def test_mine_returns_the_authors_own_publications(self):
        self.publish(visibility='link')
        response = self.client.get(
            reverse('orchestrator:template_list'), {'mine': '1'})
        self.assertEqual(len(response.data), 1)
        self.assertTrue(response.data[0]['is_mine'])

    def test_installing_a_shared_agent_creates_a_copy_owned_by_the_installer(self):
        self.publish()
        self.client.force_authenticate(user=self.viewer)
        response = self.client.post(
            reverse('orchestrator:template_install', args=['reconciler']),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        installed = SubAgent.objects.get(id=response.data['id'])
        self.assertEqual(installed.user, self.viewer)
        self.assertIn('shared:reconciler', installed.tags)
        # The author's own copy is untouched.
        self.assertEqual(SubAgent.objects.filter(user=self.author).count(), 1)

    def test_installing_a_shared_agent_counts_the_install(self):
        self.publish()
        self.client.force_authenticate(user=self.viewer)
        self.client.post(
            reverse('orchestrator:template_install', args=['reconciler']),
            {}, format='json')
        self.assertEqual(SharedAgent.objects.get().install_count, 1)

    def test_a_shared_agents_requirements_are_resolved_against_the_installer(self):
        """The check that makes installing a stranger's agent safe at all."""
        kb = KnowledgeBase.objects.create(user=self.author, name='Vendors')
        self.agent.tool_grants = {'rag': True}
        self.agent.agent_context = {'knowledgeBases': [kb.id]}
        self.agent.save()
        self.publish()

        self.client.force_authenticate(user=self.viewer)
        # The author's own KB id, offered by a malicious installer.
        response = self.client.post(
            reverse('orchestrator:template_install', args=['reconciler']),
            {'requirements': {'knowledge_base_1': kb.id}}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SubAgent.objects.filter(user=self.viewer).exists())

    def test_installing_a_withdrawn_share_is_refused(self):
        self.publish()
        self.client.delete(self.share_url)
        self.client.force_authenticate(user=self.viewer)
        response = self.client.post(
            reverse('orchestrator:template_install', args=['reconciler']),
            {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class PublicCatalogueTests(APITestCase):
    """The unauthenticated surface. Everything here is about what leaks.

    This is the second route in the app reachable with no account, after the
    webhook receiver, and it inherits that route's rule: every refusal is the
    same 404. A `link` share, a `platform` share, a withdrawn one and a slug
    that never existed have to be indistinguishable from outside, or the
    endpoint becomes a way to enumerate what people published privately.
    """

    def setUp(self):
        self.author = User.objects.create_user('author', 'a@example.com', 'pw')
        self.agent = SubAgent.objects.create(
            user=self.author, name='Reconciler', prompt='Reconcile.',
            tool_grants={'webSearch': True},
        )
        self.share_url = reverse('orchestrator:agent_share', args=[self.agent.id])
        self.list_url = reverse('orchestrator:public_agent_list')

    def publish(self, visibility):
        self.client.force_authenticate(user=self.author)
        response = self.client.post(
            self.share_url,
            {'tagline': 'Reconciles invoices.', 'visibility': visibility},
            format='json')
        assert response.status_code == status.HTTP_200_OK, response.data
        self.client.force_authenticate(user=None)
        return response

    def detail_url(self, slug='reconciler'):
        return reverse('orchestrator:public_agent_detail', args=[slug])

    # ---- what is reachable ------------------------------------------------

    def test_a_public_agent_is_readable_with_no_account(self):
        self.publish('public')
        response = self.client.get(self.detail_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['name'], 'Reconciler')

    def test_the_public_listing_needs_no_account(self):
        self.publish('public')
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([e['slug'] for e in response.data['results']],
                         ['reconciler'])
        self.assertFalse(response.data['truncated'])

    # ---- what is not ------------------------------------------------------

    def test_a_platform_share_is_not_public(self):
        """Publishing to the platform is not publishing to the internet."""
        self.publish('platform')
        self.assertEqual(self.client.get(self.detail_url()).status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.get(self.list_url).data['results'], [])

    def test_a_link_share_is_not_public(self):
        self.publish('link')
        self.assertEqual(self.client.get(self.detail_url()).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_a_withdrawn_public_agent_stops_being_public(self):
        self.publish('public')
        self.client.force_authenticate(user=self.author)
        self.client.delete(self.share_url)
        self.client.force_authenticate(user=None)
        self.assertEqual(self.client.get(self.detail_url()).status_code,
                         status.HTTP_404_NOT_FOUND)

    def test_every_refusal_is_the_same_answer(self):
        """Otherwise the endpoint is an oracle for private slugs."""
        self.publish('link')
        private = self.client.get(self.detail_url('reconciler'))
        missing = self.client.get(self.detail_url('no-such-agent'))
        self.assertEqual(private.status_code, missing.status_code)
        self.assertEqual(private.data, missing.data)

    # ---- what the payload carries -----------------------------------------

    def test_the_author_is_credited_by_name_never_by_email(self):
        self.publish('public')
        response = self.client.get(self.detail_url())
        self.assertEqual(response.data['author'], 'author')
        self.assertNotIn('@', str(response.data))

    def test_the_public_payload_carries_no_account_shaped_fields(self):
        """A narrower projection than the signed-in one, deliberately.

        `candidates` are computed from a caller, and here there is none; a
        response carrying an empty pool would be inviting a picker that cannot
        work. `is_mine` is meaningless without a viewer.
        """
        self.publish('public')
        response = self.client.get(self.detail_url())
        self.assertNotIn('is_mine', response.data)
        self.assertNotIn('visibility', response.data)
        for req in response.data['requirements']:
            self.assertNotIn('candidates', req)

    def test_the_public_payload_still_says_what_the_agent_may_do(self):
        """A visitor deciding whether to sign up needs the permission envelope."""
        self.publish('public')
        response = self.client.get(self.detail_url())
        self.assertTrue(response.data['config']['tools']['webSearch'])
        self.assertIn('autonomy', response.data['config'])

    def test_no_row_id_reaches_an_anonymous_reader(self):
        kb = KnowledgeBase.objects.create(user=self.author, name='Vendors')
        self.agent.tool_grants = {'rag': True}
        self.agent.agent_context = {'knowledgeBases': [kb.id]}
        self.agent.save()
        self.publish('public')
        response = self.client.get(self.detail_url())
        self.assertNotIn('knowledgeBases', response.data['config'])
        self.assertEqual(response.data['requirements'][0]['type'],
                         'knowledge_base')

    # ---- the signed-in side still agrees ----------------------------------

    def test_a_public_agent_is_also_listed_to_signed_in_users(self):
        """`public` is wider than `platform`, not parallel to it."""
        self.publish('public')
        viewer = User.objects.create_user('viewer', 'v@example.com', 'pw')
        self.client.force_authenticate(user=viewer)
        response = self.client.get(reverse('orchestrator:template_list'))
        self.assertIn('reconciler', {t['slug'] for t in response.data})

    def test_installing_still_requires_an_account(self):
        self.publish('public')
        response = self.client.post(
            reverse('orchestrator:template_install', args=['reconciler']),
            {}, format='json')
        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))
        self.assertEqual(SubAgent.objects.count(), 1)

    def test_publishing_publicly_is_a_deliberate_choice(self):
        """The default must never be the widest rung."""
        self.client.force_authenticate(user=self.author)
        self.client.post(self.share_url, {'tagline': 'Mine.'}, format='json')
        self.assertEqual(SharedAgent.objects.get().visibility, 'platform')
