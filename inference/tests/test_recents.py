"""
Recently opened files and saved app tabs (inference/recents.py).

What these pin:
* opening a file moves it to the top and counts the open; reopening does not
  add a second row;
* the stored `view_state` comes back on the next open, and saving it is not
  another open;
* a file someone else owns cannot be recorded, read or saved into a session,
  and every refusal is the same 404;
* a trashed or unshared file drops out of the listing without anyone
  deleting its row;
* the per-user cap and the `view_state` limits hold;
* saved tabs come back resolved to names, with foreign and trashed ids gone.
"""
from __future__ import annotations

from django.contrib.auth import get_user_model
from django.test import TestCase
from rest_framework.test import APIClient

from inference import recents, recycle
from inference.models import AppSession, Document, RecentFile

User = get_user_model()


class RecentsCase(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.them = APIClient()
        self.them.force_authenticate(self.other)

    def doc(self, name='a.pdf', user=None, file_type='pdf', **extra):
        return Document.objects.create(
            user=user or self.owner, name=name, file_type=file_type,
            content_text='x', file_size=1, status='stored', **extra,
        )

    def open(self, doc_id, client=None, **body):
        return (client or self.me).post(
            '/api/inference/recent/', {'document_id': doc_id, **body}, format='json',
        )


class RecordOpenTests(RecentsCase):
    def test_opening_moves_a_file_to_the_top_and_counts(self):
        a, b = self.doc('a.pdf'), self.doc('b.pdf')
        self.assertEqual(self.open(a.id, app='pdf').status_code, 201)
        self.open(b.id, app='pdf')
        self.open(a.id, app='pdf')
        rows = self.me.get('/api/inference/recent/').json()['results']
        self.assertEqual([r['document']['id'] for r in rows], [a.id, b.id])
        self.assertEqual(rows[0]['open_count'], 2)
        self.assertEqual(RecentFile.objects.filter(user=self.owner).count(), 2)

    def test_view_state_comes_back_on_the_next_open(self):
        a = self.doc()
        self.open(a.id, app='pdf')
        resp = self.me.patch(f'/api/inference/recent/{a.id}/', {'view_state': {'page': 7, 'zoom': 1.25}},
                             format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        again = self.open(a.id, app='pdf').json()
        self.assertEqual(again['view_state'], {'page': 7, 'zoom': 1.25})
        # Saving the position is not another open.
        self.assertEqual(again['open_count'], 2)
        self.assertEqual(self.me.get(f'/api/inference/recent/{a.id}/').json()['view_state']['page'], 7)

    def test_the_app_filter_and_the_type_filter(self):
        pdf, deck = self.doc('a.pdf'), self.doc('b.pptx', file_type='pptx')
        self.open(pdf.id, app='pdf')
        self.open(deck.id, app='slides')
        by_app = self.me.get('/api/inference/recent/?app=slides').json()['results']
        self.assertEqual([r['document']['id'] for r in by_app], [deck.id])
        by_type = self.me.get('/api/inference/recent/?types=pdf').json()['results']
        self.assertEqual([r['document']['id'] for r in by_type], [pdf.id])

    def test_forget_one_and_clear_all(self):
        a, b = self.doc('a.pdf'), self.doc('b.pdf')
        self.open(a.id)
        self.open(b.id)
        self.me.delete(f'/api/inference/recent/{a.id}/')
        self.assertEqual([r['document']['id'] for r in self.me.get('/api/inference/recent/').json()['results']],
                         [b.id])
        self.assertEqual(self.me.delete('/api/inference/recent/').json()['removed'], 1)

    def test_the_cap_drops_the_oldest(self):
        docs = [self.doc(f'{i}.pdf') for i in range(4)]
        old = recents.RECENT_CAP
        recents.RECENT_CAP = 3
        try:
            for d in docs:
                recents.record_open(self.owner, d.id)
        finally:
            recents.RECENT_CAP = old
        kept = set(RecentFile.objects.filter(user=self.owner).values_list('document_id', flat=True))
        self.assertEqual(kept, {d.id for d in docs[1:]})


class IsolationTests(RecentsCase):
    def test_someone_elses_private_file_is_a_404_everywhere(self):
        theirs = self.doc('secret.pdf', user=self.other)
        self.assertEqual(self.open(theirs.id).status_code, 404)
        self.assertEqual(self.me.get(f'/api/inference/recent/{theirs.id}/').status_code, 404)
        self.assertEqual(self.me.patch(f'/api/inference/recent/{theirs.id}/', {'view_state': {}},
                                       format='json').status_code, 404)
        # An id that does not exist answers the same.
        self.assertEqual(self.open(999999).status_code, 404)
        self.assertFalse(RecentFile.objects.exists())

    def test_a_public_library_file_can_be_opened_and_drops_out_when_unshared(self):
        shared = self.doc('lib.pdf', user=self.other, sharing_mode='shared_read')
        self.assertEqual(self.open(shared.id).status_code, 201)
        self.assertEqual(len(self.me.get('/api/inference/recent/').json()['results']), 1)
        Document.objects.filter(id=shared.id).update(sharing_mode='private')
        self.assertEqual(self.me.get('/api/inference/recent/').json()['results'], [])

    def test_a_trashed_file_leaves_the_listing(self):
        a = self.doc()
        self.open(a.id)
        recycle.trash(self.owner, documents=[a])
        self.assertEqual(self.me.get('/api/inference/recent/').json()['results'], [])
        # And it cannot be reopened while trashed.
        self.assertEqual(self.open(a.id).status_code, 404)

    def test_one_users_recents_are_not_anothers(self):
        self.open(self.doc().id)
        self.assertEqual(self.them.get('/api/inference/recent/').json()['results'], [])


class ValidationTests(RecentsCase):
    def test_bad_input_is_a_400_with_a_reason(self):
        a = self.doc()
        cases = [
            {'document_id': 'x'},
            {'document_id': True},
            {'document_id': a.id, 'app': 'Not An App!'},
            {'document_id': a.id, 'view_state': [1, 2]},
            {'document_id': a.id, 'view_state': {'nested': {'a': 1}}},
            {'document_id': a.id, 'view_state': {'big': 'x' * 3000}},
            {'document_id': a.id, 'view_state': {str(i): i for i in range(20)}},
        ]
        for body in cases:
            resp = self.me.post('/api/inference/recent/', body, format='json')
            self.assertEqual(resp.status_code, 400, body)
            self.assertIn('error', resp.json())


class AppSessionTests(RecentsCase):
    def url(self, app='docs'):
        return f'/api/inference/app-sessions/{app}/'

    def test_an_unsaved_app_has_no_tabs(self):
        self.assertEqual(self.me.get(self.url()).json(), {'app': 'docs', 'tabs': [], 'active': None,
                                                          'updated_at': None})

    def test_tabs_round_trip_resolved_to_names(self):
        a, b = self.doc('a.md', file_type='md'), self.doc('b.md', file_type='md')
        resp = self.me.put(self.url(), {'tabs': [b.id, a.id, b.id], 'active': a.id}, format='json')
        self.assertEqual(resp.status_code, 200, resp.content)
        got = self.me.get(self.url()).json()
        self.assertEqual([t['id'] for t in got['tabs']], [b.id, a.id])
        self.assertEqual(got['tabs'][0]['name'], 'b.md')
        self.assertEqual(got['active'], a.id)

    def test_foreign_and_trashed_ids_are_dropped(self):
        mine, gone = self.doc('a.md', file_type='md'), self.doc('b.md', file_type='md')
        theirs = self.doc('t.md', user=self.other, file_type='md')
        self.me.put(self.url(), {'tabs': [mine.id, theirs.id, gone.id], 'active': theirs.id}, format='json')
        stored = AppSession.objects.get(user=self.owner, app='docs')
        self.assertNotIn(theirs.id, stored.tabs)
        self.assertIsNone(stored.active)
        recycle.trash(self.owner, documents=[gone])
        self.assertEqual([t['id'] for t in self.me.get(self.url()).json()['tabs']], [mine.id])

    def test_sessions_are_per_app_and_per_user(self):
        a = self.doc('a.md', file_type='md')
        self.me.put(self.url('docs'), {'tabs': [a.id]}, format='json')
        self.assertEqual(self.me.get(self.url('notepad')).json()['tabs'], [])
        self.assertEqual(self.them.get(self.url('docs')).json()['tabs'], [])

    def test_bad_tabs_are_a_400(self):
        for body in ({'tabs': 'nope'}, {'tabs': [1, 'x']}, {'tabs': [], 'active': 'x'}):
            self.assertEqual(self.me.put(self.url(), body, format='json').status_code, 400, body)

    def test_the_tab_cap_holds(self):
        docs = [self.doc(f'{i}.md', file_type='md') for i in range(recents.MAX_TABS + 5)]
        self.me.put(self.url(), {'tabs': [d.id for d in docs]}, format='json')
        self.assertEqual(len(self.me.get(self.url()).json()['tabs']), recents.MAX_TABS)
