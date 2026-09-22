"""The office pack: one click installs the three file specialists.

Idempotent via `SubAgent.template_slug`: reinstalling skips what is already
there rather than suffixing a second copy. Templates with required
requirements are never installed by the pack — they are listed as needing
setup, with a link to the normal install screen.
"""
from django.contrib.auth.models import User
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from agents import gallery
from agents.models import SubAgent


class InstallPackTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user('packer', 'p@example.com', 'pw')
        self.client.force_authenticate(user=self.user)

    def _pack(self, pack="office"):
        return self.client.post(
            reverse('orchestrator:template_install_pack'),
            {"pack": pack}, format='json',
        )

    def test_the_office_pack_installs_three_specialists(self):
        response = self._pack()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['pack'], 'office')
        self.assertEqual(
            sorted(i['slug'] for i in response.data['installed']),
            ['analyst', 'slides', 'writer'],
        )
        self.assertEqual(response.data['skipped'], [])
        slugs = set(
            SubAgent.objects.filter(user=self.user)
            .values_list('template_slug', flat=True)
        )
        self.assertEqual(slugs, {'analyst', 'slides', 'writer'})

    def test_reinstalling_is_idempotent(self):
        first = self._pack()
        self.assertEqual(len(first.data['installed']), 3)
        second = self._pack()
        self.assertEqual(second.data['installed'], [])
        self.assertEqual(
            sorted(s['slug'] for s in second.data['skipped']),
            ['analyst', 'slides', 'writer'],
        )
        self.assertTrue(
            all(s['reason'] == 'already installed' for s in second.data['skipped'])
        )
        self.assertEqual(SubAgent.objects.filter(user=self.user).count(), 3)

    def test_single_install_marks_the_slug_so_the_pack_skips_it(self):
        self.client.post(
            reverse('orchestrator:template_install', args=['analyst']), {},
            format='json',
        )
        response = self._pack()
        installed = sorted(i['slug'] for i in response.data['installed'])
        self.assertEqual(installed, ['slides', 'writer'])
        skipped = {s['slug']: s['reason'] for s in response.data['skipped']}
        self.assertEqual(skipped.get('analyst'), 'already installed')

    def test_an_unknown_pack_is_404(self):
        response = self._pack(pack="nope")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertFalse(SubAgent.objects.filter(user=self.user).exists())

    def test_ownership_installing_twice_suffixes_but_pack_skips(self):
        # A second *single* install suffixes the name (ordinary thing to do);
        # the pack still skips on the slug, not the name.
        for _ in range(2):
            self.client.post(
                reverse('orchestrator:template_install', args=['slides']), {},
                format='json',
            )
        self.assertEqual(
            SubAgent.objects.filter(user=self.user, template_slug='slides').count(), 2
        )
        response = self._pack()
        installed = [i['slug'] for i in response.data['installed']]
        self.assertNotIn('slides', installed)

    def test_the_pack_members_are_all_requirement_free(self):
        # The contract the endpoint promises: everything in a pack installs
        # with an empty body. A required requirement added later must fail
        # here rather than silently becoming "needs setup".
        for pack, members in gallery.PACKS.items():
            for slug in members:
                entry = gallery.get(slug)
                self.assertIsNotNone(entry, f'{pack} names {slug}, which is gone')
                required = [r for r in entry.get('requirements') or []
                            if not r.get('optional')]
                self.assertEqual(required, [], f'{slug} gained a required requirement')

    def test_every_pack_installs_fully_with_an_empty_body(self):
        """One click, no setup screen: the whole promise of a pack."""
        for pack, members in gallery.PACKS.items():
            with self.subTest(pack=pack):
                response = self._pack(pack=pack)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.assertEqual(
                    sorted(i['slug'] for i in response.data['installed']),
                    sorted(members),
                )
                self.assertEqual(response.data['skipped'], [])

    def test_every_pack_reinstall_is_idempotent(self):
        for pack, members in gallery.PACKS.items():
            with self.subTest(pack=pack):
                self._pack(pack=pack)
                second = self._pack(pack=pack)
                self.assertEqual(second.data['installed'], [])
                self.assertEqual(
                    sorted(s['slug'] for s in second.data['skipped']),
                    sorted(members),
                )


class CodePackTests(APITestCase):
    """The coding team installs as one pack, wired to its lead."""

    def setUp(self):
        self.user = User.objects.create_user('coder', 'c@example.com', 'pw')
        self.client.force_authenticate(user=self.user)

    def _pack(self, **body):
        return self.client.post(
            reverse('orchestrator:template_install_pack'),
            {'pack': 'code', **body}, format='json',
        )

    def test_the_code_pack_installs_the_roster_and_the_lead(self):
        response = self._pack()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            sorted(i['slug'] for i in response.data['installed']),
            sorted(gallery.PACKS['code']),
        )

    def test_the_lead_is_scoped_to_the_installed_roster(self):
        self._pack()
        lead = SubAgent.objects.get(user=self.user, template_slug='coding-lead')
        roster_ids = set(SubAgent.objects.filter(
            user=self.user,
            template_slug__in=[s for s in gallery.PACKS['code']
                               if s not in ('coding-lead', 'repo-assistant')],
        ).values_list('id', flat=True))
        self.assertEqual(set(lead.agent_context.get('delegatesTo') or []), roster_ids)
        self.assertNotIn(lead.id, lead.agent_context.get('delegatesTo') or [])

    def test_the_pack_installs_with_a_tightened_implementer(self):
        """The install-screen matrix: per-template tightening before the rows
        are written — and the tightening is enforced, not just stored."""
        response = self._pack(overrides={
            'code-implementer': {'autonomy': 'ask', 'commandScope': ['test']},
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        impl = SubAgent.objects.get(user=self.user, template_slug='code-implementer')
        self.assertEqual(impl.guardrails.get('autonomy'), 'ask')
        self.assertEqual(impl.agent_context.get('commandScope'), ['test'])

    def test_a_bad_override_skips_only_that_template(self):
        response = self._pack(overrides={
            'code-implementer': {'commandScope': ['teleport']},
        })
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        skipped = {s['slug']: s['reason'] for s in response.data['skipped']}
        self.assertEqual(skipped.get('code-implementer'), 'invalid configuration')
        self.assertFalse(SubAgent.objects.filter(
            user=self.user, template_slug='code-implementer').exists())
        # The lead still wired to whoever did install.
        lead = SubAgent.objects.get(user=self.user, template_slug='coding-lead')
        self.assertTrue(lead.agent_context.get('delegatesTo'))
