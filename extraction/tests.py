from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from extraction.models import ExtractedRow, ExtractionSchema

FIELDS = [
    {'name': 'vendor', 'type': 'string'},
    {'name': 'total', 'type': 'currency'},
]


class SchemaTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.url = '/api/extraction/schemas/'

    def test_create(self):
        r = self.client.post(
            self.url, {'name': 'Purchase invoices', 'fields': FIELDS}, format='json'
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual(r.data['field_count'], 2)

    def test_schema_needs_at_least_one_field(self):
        r = self.client.post(self.url, {'name': 'Empty', 'fields': []}, format='json')
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_duplicate_field_names_are_refused(self):
        r = self.client.post(
            self.url,
            {'name': 'Dupes', 'fields': [{'name': 'total'}, {'name': 'total'}]},
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn('total', str(r.data))

    def test_unknown_field_type_is_refused(self):
        r = self.client.post(
            self.url, {'name': 'Odd', 'fields': [{'name': 'x', 'type': 'blob'}]}, format='json'
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_threshold_must_be_a_fraction(self):
        r = self.client.post(
            self.url,
            {'name': 'Loud', 'fields': FIELDS, 'confidence_threshold': 80},
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)


class ReviewQueueTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS, confidence_threshold=0.8
        )

    def _row(self, confidence, **kw):
        row = ExtractedRow.objects.create(
            schema=self.schema, document_name='inv.pdf',
            data={'vendor': 'Shree', 'total': '8650'}, confidence=confidence, **kw
        )
        row.apply_threshold()
        row.save(update_fields=['status'])
        return row

    def test_low_confidence_rows_are_held(self):
        self.assertEqual(self._row(0.62).status, 'needs_review')
        self.assertEqual(self._row(0.99).status, 'accepted')

    def test_posting_rows_applies_the_threshold(self):
        r = self.client.post(
            f'/api/extraction/schemas/{self.schema.id}/rows/',
            [{'document_name': 'a.pdf', 'data': {}, 'confidence': 0.4},
             {'document_name': 'b.pdf', 'data': {}, 'confidence': 0.95}],
            format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_201_CREATED)
        self.assertEqual([x['status'] for x in r.data], ['needs_review', 'accepted'])

    def test_raising_the_threshold_resorts_existing_rows(self):
        row = self._row(0.85)
        self.assertEqual(row.status, 'accepted')

        self.client.patch(
            f'/api/extraction/schemas/{self.schema.id}/',
            {'confidence_threshold': 0.9}, format='json',
        )
        row.refresh_from_db()
        self.assertEqual(row.status, 'needs_review',
                         'the review count on the card must describe the current rule')

    def test_review_records_who_cleared_it(self):
        row = self._row(0.5)
        r = self.client.post(f'/api/extraction/rows/{row.id}/review/')
        self.assertEqual(r.data['status'], 'reviewed')

        row.refresh_from_db()
        self.assertEqual(row.reviewed_by, self.user)
        self.assertIsNotNone(row.reviewed_at)

    def test_review_with_a_correction_reports_that_it_changed(self):
        row = self._row(0.5)
        r = self.client.post(
            f'/api/extraction/rows/{row.id}/review/',
            {'data': {'total': '8750'}}, format='json',
        )
        self.assertTrue(r.data['corrected'])
        self.assertEqual(r.data['data']['total'], '8750')
        self.assertEqual(r.data['data']['vendor'], 'Shree', 'unsent fields must survive')

    def test_review_rejects_fields_not_on_the_schema(self):
        row = self._row(0.5)
        r = self.client.post(
            f'/api/extraction/rows/{row.id}/review/',
            {'data': {'not_a_field': 'x'}}, format='json',
        )
        self.assertEqual(r.status_code, status.HTTP_400_BAD_REQUEST)

    def test_a_reviewed_row_is_not_re_flagged_by_a_threshold_change(self):
        row = self._row(0.5)
        self.client.post(f'/api/extraction/rows/{row.id}/review/')
        self.client.patch(
            f'/api/extraction/schemas/{self.schema.id}/',
            {'confidence_threshold': 0.99}, format='json',
        )
        row.refresh_from_db()
        self.assertEqual(row.status, 'reviewed', 'a human judgement is not overturned by a knob')
