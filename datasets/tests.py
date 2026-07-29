from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from datasets.models import Dataset, DatasetRow


class DatasetTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.client.force_authenticate(user=self.user)
        self.url = '/api/datasets/'

    def test_create_and_list(self):
        r = self.client.post(self.url, {'name': 'Invoice fields — gold'}, format='json')
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['split_label'], '80/10/10')

        listed = self.client.get(self.url)
        self.assertEqual(len(listed.data['results']), 1)

    def test_split_must_add_up(self):
        r = self.client.post(
            self.url, {'name': 'Bad split', 'train_pct': 80, 'val_pct': 30, 'test_pct': 10},
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_all_zero_split_is_allowed(self):
        r = self.client.post(
            self.url, {'name': 'No split', 'train_pct': 0, 'val_pct': 0, 'test_pct': 0},
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['split_label'], '—')

    def test_rows_are_paged_not_inlined(self):
        ds = Dataset.objects.create(user=self.user, name='Big')
        DatasetRow.objects.bulk_create(
            [DatasetRow(dataset=ds, inputs={'i': i}, expected={'o': i}) for i in range(120)]
        )
        detail = self.client.get(f'{self.url}{ds.id}/')
        self.assertEqual(detail.data['row_count'], 120)
        self.assertNotIn('rows', detail.data)

        rows = self.client.get(f'{self.url}{ds.id}/rows/')
        self.assertEqual(rows.data['count'], 120)
        self.assertEqual(len(rows.data['results']), 50)

    def test_rows_filter_by_split(self):
        ds = Dataset.objects.create(user=self.user, name='Split')
        DatasetRow.objects.create(dataset=ds, split='train')
        DatasetRow.objects.create(dataset=ds, split='test')
        r = self.client.get(f'{self.url}{ds.id}/rows/?split=test')
        self.assertEqual(r.data['count'], 1)

    def test_stats(self):
        ds = Dataset.objects.create(user=self.user, name='Stats')
        DatasetRow.objects.create(dataset=ds, split='train')
        DatasetRow.objects.create(dataset=ds, split='train')
        DatasetRow.objects.create(dataset=ds, split='val')
        r = self.client.get(f'{self.url}{ds.id}/stats/')
        self.assertEqual(r.data, {'total': 3, 'train': 2, 'val': 1, 'test': 0})

    def test_another_users_dataset_is_invisible(self):
        theirs = Dataset.objects.create(user=self.other, name='Theirs')
        self.assertEqual(self.client.get(f'{self.url}{theirs.id}/').status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertEqual(len(self.client.get(self.url).data['results']), 0)
