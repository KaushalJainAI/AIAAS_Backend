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
        for slug in gallery.PACKS['office']:
            entry = gallery.get(slug)
            self.assertIsNotNone(entry)
            required = [r for r in entry.get('requirements') or [] if not r.get('optional')]
            self.assertEqual(required, [], f'{slug} gained a required requirement')
