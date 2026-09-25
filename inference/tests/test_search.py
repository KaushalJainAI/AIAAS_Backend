"""
Search for the Documents explorer: exact matches first, close matches after.

Tier 1 is the database (`icontains` on the name and the stored text, every
query word). Tier 2 is conservative fuzzy matching on names only, in Python
with stdlib `difflib` — contents are never fuzzy-matched. The two tiers are
returned separately so the UI can label the second one honestly: a near-miss
offered as a hit is the failure `vfs.find` warns about.
"""
from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from inference import filesystem as fs
from inference import recycle
from inference import search as doc_search
from inference.models import Document, Folder

URL = '/api/inference/documents/search/'


class SearchFixture(APITestCase):
    """One user with a small tree; a stranger with their own file."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw12345')
        self.other = User.objects.create_user(username='stranger', password='pw12345')
        self.client.force_authenticate(user=self.user)

        self.reports = fs.create_folder(self.user, 'Reports', None)
        self.archive = fs.create_folder(self.user, 'Archive', None)
        self.q1 = Document.objects.create(
            user=self.user, name='Quarterly Report Q1.xlsx', file_type='xlsx',
            file_size=10, content_text='revenue figures for the first quarter',
            folder=self.reports,
        )
        self.notes = Document.objects.create(
            user=self.user, name='meeting-notes.md', file_type='md',
            file_size=10, content_text='discussed the quarterly invoice process',
            folder=self.archive,
        )
        self.other_doc = Document.objects.create(
            user=self.other, name='Quarterly Report Other.xlsx', file_type='xlsx',
            file_size=10, content_text='somebody elses numbers',
            folder=None,
        )

    def _search(self, **params):
        return self.client.get(URL, params)


class ExactTests(SearchFixture):

    def test_exact_name_hit(self):
        response = self._search(q='Quarterly')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d['filename'] for d in response.data['exact']]
        self.assertIn('Quarterly Report Q1.xlsx', names)
        hit = next(d for d in response.data['exact']
                   if d['filename'] == 'Quarterly Report Q1.xlsx')
        self.assertEqual(hit['matched_in'], 'name')
        self.assertIsNone(hit['snippet'])
        self.assertEqual(response.data['fuzzy'], [])

    def test_multi_word_out_of_order(self):
        # "report quarterly" finds "Quarterly Report", which a single
        # whole-string icontains would not.
        response = self._search(q='report quarterly')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d['filename'] for d in response.data['exact']]
        self.assertIn('Quarterly Report Q1.xlsx', names)

    def test_content_only_hit_carries_a_capped_snippet(self):
        response = self._search(q='invoice')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        hit = next(d for d in response.data['exact']
                   if d['filename'] == 'meeting-notes.md')
        self.assertEqual(hit['matched_in'], 'content')
        self.assertIsNotNone(hit['snippet'])
        self.assertIn('invoice', hit['snippet'].lower())
        self.assertLessEqual(len(hit['snippet']), 165)

    def test_phrase_in_name_ranks_above_content_hit(self):
        Document.objects.create(
            user=self.user, name='random.txt', file_type='txt', file_size=1,
            content_text='this file mentions Quarterly Report in passing',
            folder=None,
        )
        response = self._search(q='Quarterly Report')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        first = response.data['exact'][0]
        self.assertEqual(first['filename'], 'Quarterly Report Q1.xlsx')

    def test_short_query_answers_empty_200(self):
        response = self._search(q='x')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['exact'], [])
        self.assertEqual(response.data['fuzzy'], [])
        self.assertEqual(response.data['count'], 0)

    def test_other_users_documents_never_appear(self):
        response = self._search(q='Quarterly')
        names = ([d['filename'] for d in response.data['exact']]
                 + [d['filename'] for d in response.data['fuzzy']])
        self.assertNotIn('Quarterly Report Other.xlsx', names)


class ScopeTests(SearchFixture):

    def test_folder_scope_covers_subtree_not_siblings(self):
        child = fs.create_folder(self.user, 'Child', self.reports)
        deep = Document.objects.create(
            user=self.user, name='deep quarterly numbers.txt', file_type='txt',
            file_size=1, content_text='nested', folder=child,
        )
        response = self._search(q='quarterly', folder_id=self.reports.id)
        names = [d['filename'] for d in response.data['exact']]
        self.assertIn('Quarterly Report Q1.xlsx', names)
        self.assertIn(deep.name, names)
        self.assertNotIn('meeting-notes.md', names)

    def test_foreign_folder_is_404(self):
        theirs = fs.create_folder(self.other, 'Theirs', None)
        response = self._search(q='quarterly', folder_id=theirs.id)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_eval_tree_is_hidden(self):
        hidden_root = fs.create_folder(self.user, fs.EVAL_ROOT_NAME, None)
        Document.objects.create(
            user=self.user, name='eval quarterly fixture.txt', file_type='txt',
            file_size=1, content_text='eval quarterly words', folder=hidden_root,
        )
        response = self._search(q='quarterly')
        names = ([d['filename'] for d in response.data['exact']]
                 + [d['filename'] for d in response.data['fuzzy']])
        self.assertNotIn('eval quarterly fixture.txt', names)

    def test_trashed_files_are_excluded(self):
        recycle.trash(self.user, documents=[self.q1])
        response = self._search(q='Quarterly')
        names = ([d['filename'] for d in response.data['exact']]
                 + [d['filename'] for d in response.data['fuzzy']])
        self.assertNotIn('Quarterly Report Q1.xlsx', names)

    def test_types_filter(self):
        response = self._search(q='quarterly', types='md')
        names = [d['filename'] for d in response.data['exact']]
        self.assertNotIn('Quarterly Report Q1.xlsx', names)
        self.assertIn('meeting-notes.md', names)

    def test_public_scope(self):
        self.q1.sharing_mode = 'shared_read'
        self.q1.save(update_fields=['sharing_mode'])
        self.client.force_authenticate(user=self.other)
        response = self._search(q='Quarterly', scope='public')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [d['filename'] for d in response.data['exact']]
        self.assertIn('Quarterly Report Q1.xlsx', names)
        self.assertEqual(response.data['folders'], [])


class FuzzyTests(SearchFixture):

    def test_typo_finds_close_match(self):
        response = self._search(q='quaterly repot')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['exact'], [])
        fuzzy = [d['filename'] for d in response.data['fuzzy']]
        self.assertIn('Quarterly Report Q1.xlsx', fuzzy)
        hit = next(d for d in response.data['fuzzy']
                   if d['filename'] == 'Quarterly Report Q1.xlsx')
        self.assertEqual(hit['matched_in'], 'fuzzy')
        self.assertIn('score', hit)

    def test_fuzzy_non_match_stays_empty(self):
        response = self._search(q='xqzwv')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['exact'], [])
        self.assertEqual(response.data['fuzzy'], [])

    def test_short_unrelated_word_is_not_a_match(self):
        # "cat" must not fuzzy-match "car report": short words need a prefix.
        Document.objects.create(
            user=self.user, name='car report.txt', file_type='txt', file_size=1,
            content_text='nothing relevant', folder=None,
        )
        response = self._search(q='cat')
        fuzzy = [d['filename'] for d in response.data['fuzzy']]
        self.assertNotIn('car report.txt', fuzzy)

    def test_no_id_in_both_tiers(self):
        response = self._search(q='quaterly')
        exact_ids = {d['id'] for d in response.data['exact']}
        fuzzy_ids = {d['id'] for d in response.data['fuzzy']}
        self.assertTrue(exact_ids.isdisjoint(fuzzy_ids))

    def test_misspelled_folder_returns_fuzzy_folder_hit(self):
        response = self._search(q='reprots')
        folders = response.data['folders']
        self.assertTrue(any(f['name'] == 'Reports' for f in folders))
        hit = next(f for f in folders if f['name'] == 'Reports')
        self.assertEqual(hit['matched_in'], 'fuzzy')

    def test_limit_truncates_with_note(self):
        for i in range(6):
            Document.objects.create(
                user=self.user, name=f'truncate probe {i}.txt', file_type='txt',
                file_size=1, content_text='probe words', folder=None,
            )
        response = self._search(q='probe', limit=3)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['exact']), 3)
        self.assertTrue(response.data['truncated'])
        self.assertIsNotNone(response.data['note'])


class FuzzyUnitTests(TestCase):
    """The scoring function on its own: prefixes pass, loose words fail."""

    def test_prefix_is_full_match(self):
        self.assertEqual(doc_search.word_score('quart', 'quarterly'), 1.0)

    def test_transposition_scores_high(self):
        self.assertGreaterEqual(doc_search.word_score('repot', 'report'), 0.75)

    def test_short_words_rejected(self):
        # A two-letter prefix is an as-you-type match; a two-letter
        # non-prefix is not a match at all.
        self.assertEqual(doc_search.word_score('ca', 'car'), 1.0)
        self.assertEqual(doc_search.word_score('cx', 'car'), 0.0)

    def test_every_query_word_must_match(self):
        self.assertEqual(
            doc_search.fuzzy_score(['quarterly', 'xqzwv'],
                                   'Quarterly Report Q1.xlsx'), 0.0)
        self.assertGreater(
            doc_search.fuzzy_score(['quaterly', 'repot'],
                                   'Quarterly Report Q1.xlsx'), 0)

    def test_extension_is_ignored(self):
        self.assertGreater(
            doc_search.fuzzy_score(['quarterly', 'report'],
                                   'Quarterly Report Q1.xlsx'), 0)
