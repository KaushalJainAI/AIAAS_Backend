from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from datasets.models import Dataset, DatasetRow
from tuning.models import TuningJob


def dataset_with_rows(user, n=20, name='Gold'):
    ds = Dataset.objects.create(user=user, name=name)
    DatasetRow.objects.bulk_create([DatasetRow(dataset=ds) for _ in range(n)])
    return ds


class TuningJobTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='other', password='pw')
        self.client.force_authenticate(user=self.user)
        self.url = '/api/tuning/jobs/'

    def test_create_queues_a_job(self):
        ds = dataset_with_rows(self.user)
        r = self.client.post(
            self.url,
            {'name': 'invoice-extract-v3', 'base_model': 'gpt-5.6-luna', 'dataset': ds.id},
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['status'], 'queued')
        self.assertEqual(r.data['dataset_rows'], 20)

    def test_too_few_rows_is_refused(self):
        ds = dataset_with_rows(self.user, n=4, name='Thin')
        r = self.client.post(
            self.url, {'name': 'doomed', 'base_model': 'm', 'dataset': ds.id}, format='json'
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('4 rows', str(r.data))

    def test_cannot_tune_on_another_users_dataset(self):
        theirs = dataset_with_rows(self.other, name='Theirs')
        r = self.client.post(
            self.url, {'name': 'sneaky', 'base_model': 'm', 'dataset': theirs.id}, format='json'
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_savings_are_computed_from_stored_baselines(self):
        job = TuningJob.objects.create(
            user=self.user, name='j', base_model='m', dataset=dataset_with_rows(self.user),
            accuracy=96.1, baseline_accuracy=94.2,
            cost_per_1k_paise=42, baseline_cost_per_1k_paise=210,
        )
        self.assertEqual(job.accuracy_delta, 1.9)
        self.assertEqual(job.cost_saving_pct, 80.0)

    def test_unscored_job_reports_none_not_zero(self):
        job = TuningJob.objects.create(
            user=self.user, name='j', base_model='m', dataset=dataset_with_rows(self.user)
        )
        self.assertIsNone(job.accuracy_delta)
        self.assertIsNone(job.cost_saving_pct)

    def test_deployed_job_cannot_be_deleted(self):
        job = TuningJob.objects.create(
            user=self.user, name='j', base_model='m',
            dataset=dataset_with_rows(self.user), status='deployed',
        )
        r = self.client.delete(f'{self.url}{job.id}/')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue(TuningJob.objects.filter(id=job.id).exists())

    def test_dataset_behind_a_job_cannot_be_deleted(self):
        """PROTECT: the examples are the only record of why the model behaves as it does."""
        from django.db.models import ProtectedError
        ds = dataset_with_rows(self.user)
        TuningJob.objects.create(user=self.user, name='j', base_model='m', dataset=ds)
        with self.assertRaises(ProtectedError):
            ds.delete()

    def test_cancel_only_applies_to_unfinished_jobs(self):
        job = TuningJob.objects.create(
            user=self.user, name='j', base_model='m',
            dataset=dataset_with_rows(self.user), status='deployed',
        )
        r = self.client.post(f'{self.url}{job.id}/cancel/')
        self.assertEqual(r.status_code, status.HTTP_409_CONFLICT)
