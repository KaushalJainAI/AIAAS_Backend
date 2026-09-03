"""
Template gallery tests.

Two things are worth testing here and they are not the listing. The first is
that the catalogue is *portable* — no template carries an id from whoever wrote
it, and none asks for a resource it was not granted the tools to use; a
malformed entry is a dropdown that installs a broken agent, and it is caught
here rather than by whoever installs it. The second is that install enforces
exactly what the builder enforces, because install is a second write path onto
the same columns and the whole safety story of a shared agent is that the
screen you approved is the configuration that runs.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import KnowledgeBase
from mcp_integration.models import MCPServer

from agents import gallery
from agents.models import SubAgent, Trigger


def curated(name):
    """The id of a curated connection; `mcp_integration.0005` seeds these."""
    return MCPServer.objects.get(name=name, user__isnull=True).id


class CatalogueIntegrityTests(APITestCase):
    """The rules that make a template installable in somebody else's account."""

    def test_the_catalogue_is_well_formed(self):
        self.assertEqual(gallery.check_catalogue(), [])

    def test_no_template_carries_a_row_id(self):
        # The failure this prevents is silent: installed elsewhere, knowledge
        # base 2 is not missing, it is *someone else's* knowledge base 2.
        for slug, entry in gallery.TEMPLATES.items():
            for field in gallery.REQUIREMENT_FIELDS.values():
                self.assertNotIn(field, entry['config'], f'{slug} leaks {field}')

    def test_every_template_config_is_accepted_by_the_agent_serializer(self):
        """A template the builder would refuse can never be installed.

        This is the check that keeps `gallery.py` honest as the serializer
        gains rules: a new validation lands here as a failing test rather than
        as a 400 the first user to click Install discovers.
        """
        from agents.views.agents import AgentSerializer

        user = User.objects.create_user('cat', 'cat@example.com', 'pw')
        kb = KnowledgeBase.objects.create(user=user, name='Corpus')
        request = type('R', (), {'user': user})()

        for slug, entry in gallery.TEMPLATES.items():
            config = dict(entry['config'])
            # Satisfy every requirement with something this user owns, since
            # a required one left blank is a shape error, not a config error.
            for req in entry['requirements']:
                field = gallery.REQUIREMENT_FIELDS[req['type']]
                if req['type'] == 'knowledge_base':
                    config.setdefault(field, []).append(kb.id)
                elif req['type'] == 'connector':
                    config.setdefault(field, []).append(curated('Gmail'))
            serializer = AgentSerializer(data=config, context={'request': request})
            self.assertTrue(
                serializer.is_valid(),
                f'{slug} is not a valid AgentConfig: {serializer.errors}',
            )


class GalleryReadTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('reader', 'r@example.com', 'pw')
        self.client.force_authenticate(user=self.user)
        self.kb = KnowledgeBase.objects.create(user=self.user, name='Handbook')

    def test_listing_requires_authentication(self):
        self.client.force_authenticate(user=None)
        response = self.client.get(reverse('orchestrator:template_list'))
        self.assertIn(response.status_code,
                      (status.HTTP_401_UNAUTHORIZED, status.HTTP_403_FORBIDDEN))

    def test_listing_returns_the_catalogue(self):
        response = self.client.get(reverse('orchestrator:template_list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), len(gallery.TEMPLATES))
        slugs = {t['slug'] for t in response.data}
        self.assertEqual(slugs, set(gallery.TEMPLATES))

    def test_a_requirement_carries_the_callers_own_candidates(self):
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['document-qa'])
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        requirement = response.data['requirements'][0]
        self.assertEqual(requirement['type'], 'knowledge_base')
        self.assertEqual([c['id'] for c in requirement['candidates']],
                         [self.kb.id])

    def test_another_users_rows_are_never_offered(self):
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        KnowledgeBase.objects.create(user=other, name='Theirs')
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['document-qa'])
        )
        labels = {c['label']
                  for c in response.data['requirements'][0]['candidates']}
        self.assertEqual(labels, {'Handbook'})

    def test_a_provider_hint_reorders_rather_than_filters(self):
        """The hint must not hide a connection the user would have picked.

        Someone whose mailbox connection is a self-hosted row with no icon
        slug still has to be able to choose it, so the pool stays complete and
        only its order changes.
        """
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['inbox-triage'])
        )
        candidates = response.data['requirements'][0]['candidates']
        self.assertEqual(candidates[0]['icon_slug'], 'gmail')
        self.assertGreater(len(candidates), 1)

    def test_unknown_slug_is_404(self):
        response = self.client.get(
            reverse('orchestrator:template_detail', args=['no-such-thing'])
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class InstallTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('installer', 'i@example.com', 'pw')
        self.client.force_authenticate(user=self.user)
        self.kb = KnowledgeBase.objects.create(user=self.user, name='Handbook')

    def install(self, slug, **body):
        return self.client.post(
            reverse('orchestrator:template_install', args=[slug]), body,
            format='json',
        )

    def test_installing_creates_an_agent_owned_by_the_caller(self):
        response = self.install('deep-research')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        agent = SubAgent.objects.get(id=response.data['id'])
        self.assertEqual(agent.user, self.user)
        self.assertEqual(agent.name, 'Deep research')

    def test_the_installed_grants_are_the_full_closed_set(self):
        """An absent key must read as denied, never as "unset"."""
        response = self.install('deep-research')
        agent = SubAgent.objects.get(id=response.data['id'])
        from agents.views.agents import TOOL_KEYS

        self.assertEqual(set(agent.tool_grants), TOOL_KEYS)
        self.assertTrue(agent.tool_grants['webSearch'])
        self.assertFalse(agent.tool_grants['shell'])

    def test_the_screen_and_the_stored_agent_agree(self):
        """What the install screen renders is what the runtime enforces.

        The template's `config` *is* the permissions screen's source, so it
        has to be the configuration that lands. If these ever diverge the
        screen is a promise nothing keeps.
        """
        listed = self.client.get(
            reverse('orchestrator:template_detail', args=['inbox-triage'])
        ).data['config']
        response = self.install('inbox-triage',
                                requirements={'mailbox': curated('Gmail')})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['autonomy'], listed['autonomy'])
        self.assertEqual(response.data['egress'], listed['egress'])
        self.assertEqual(response.data['spendCapRupees'],
                         listed['spendCapRupees'])
        self.assertEqual(response.data['tools']['mcp'], True)

    def test_a_required_requirement_must_be_answered(self):
        response = self.install('document-qa')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('Documents to answer from', response.data['error'])
        self.assertFalse(SubAgent.objects.filter(user=self.user).exists())

    def test_an_optional_requirement_may_be_left_blank(self):
        response = self.install('weekly-report',
                                timezone='Asia/Kolkata')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['knowledgeBases'], [])

    def test_a_resolved_requirement_lands_on_the_agent(self):
        response = self.install('document-qa',
                                requirements={'corpus': self.kb.id})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['knowledgeBases'], [self.kb.id])

    def test_you_cannot_satisfy_a_requirement_with_someone_elses_row(self):
        """The check that makes a shared template safe to install at all."""
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        theirs = KnowledgeBase.objects.create(user=other, name='Theirs')
        response = self.install('document-qa', requirements={'corpus': theirs.id})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SubAgent.objects.filter(user=self.user).exists())

    def test_a_scheduled_template_arms_a_builder_trigger_in_the_given_zone(self):
        response = self.install('weekly-report', timezone='Asia/Kolkata')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        agent = SubAgent.objects.get(id=response.data['id'])
        trigger = Trigger.objects.get(subagent=agent, mode='schedule')
        self.assertEqual(trigger.config['cron'], '0 9 * * 1')
        self.assertEqual(trigger.timezone, 'Asia/Kolkata')
        # `origin='builder'` so the agent's own schedule field round-trips it,
        # rather than the builder deleting it on the first save.
        self.assertEqual(trigger.origin, 'builder')
        self.assertIsNotNone(trigger.next_due_at)
        self.assertTrue(agent.allow_unattended)

    def test_a_bad_timezone_is_refused_rather_than_stored(self):
        response = self.install('weekly-report', timezone='Mars/Olympus')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(SubAgent.objects.filter(user=self.user).exists())

    def test_installing_twice_suffixes_the_name(self):
        first = self.install('deep-research')
        second = self.install('deep-research')
        self.assertEqual(second.status_code, status.HTTP_201_CREATED)
        self.assertEqual(first.data['name'], 'Deep research')
        self.assertEqual(second.data['name'], 'Deep research (1)')

    def test_the_name_can_be_overridden_at_install(self):
        response = self.install('deep-research', name='Market research')
        self.assertEqual(response.data['name'], 'Market research')

    def test_a_suspicious_name_is_refused_the_same_as_in_the_builder(self):
        response = self.install('deep-research', name='../../etc/passwd')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_an_install_records_its_first_revision(self):
        """Otherwise the first edit reads as inventing the whole config."""
        from logs.models import SubAgentRevision

        response = self.install('deep-research')
        agent = SubAgent.objects.get(id=response.data['id'])
        revision = SubAgentRevision.objects.get(subagent=agent)
        self.assertEqual(revision.number, 1)
        self.assertEqual(revision.config['autonomy'], 'full')

    def test_the_agent_is_tagged_with_the_template_it_came_from(self):
        response = self.install('deep-research')
        agent = SubAgent.objects.get(id=response.data['id'])
        self.assertIn('template:deep-research', agent.tags)

    def test_unknown_slug_is_404(self):
        self.assertEqual(self.install('no-such-thing').status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertFalse(SubAgent.objects.filter(user=self.user).exists())
