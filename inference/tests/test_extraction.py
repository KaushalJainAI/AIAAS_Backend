"""
Extraction schema/row API tests, ported from the retired `extraction` app
(2026-08-18). The surface is unchanged: `/api/extraction/` still lives here.

The review queue is the part under test that matters: a held row is an
explicit human decision recorded against a user, and a threshold change has to
re-sort the rows already collected.
"""
from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from inference.models import ExtractedRow, ExtractionSchema

SCHEMA_URL = '/api/extraction/schemas/'
ROWS_URL = '/api/extraction/rows/'

FIELDS = [
    {'name': 'vendor', 'label': 'Vendor', 'type': 'string', 'required': True},
    {'name': 'total', 'label': 'Total', 'type': 'currency', 'required': True},
]


class ExtractionSchemaTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)

    def test_schema_create_and_list_are_user_scoped(self):
        self.client.post(SCHEMA_URL, {
            'name': 'Invoices', 'fields': FIELDS, 'confidence_threshold': 0.8,
        }, format='json')
        ExtractionSchema.objects.create(user=self.other, name='Theirs', fields=FIELDS)

        response = self.client.get(SCHEMA_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        names = [s['name'] for s in response.data['results']]
        self.assertEqual(names, ['Invoices'])
        self.assertEqual(response.data['results'][0]['field_count'], 2)

    def test_schema_requires_fields_and_duplicate_names_are_rejected(self):
        for payload, expected in [
            ({'name': 'Empty', 'fields': []}, 'at least one field'),
            ({'name': 'Dup', 'fields': [FIELDS[0], FIELDS[0]]}, 'Duplicate field names'),
            ({'name': 'Bad type', 'fields': [{'name': 'x', 'type': 'blob'}]}, 'not one of'),
            ({'name': 'Over', 'fields': FIELDS, 'confidence_threshold': 1.5},
             'between 0 and 1'),
        ]:
            response = self.client.post(SCHEMA_URL, payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload['name'])
            self.assertIn(expected, str(response.data))

    def test_other_users_schema_is_invisible(self):
        schema = ExtractionSchema.objects.create(user=self.other, name='Theirs', fields=FIELDS)
        self.assertEqual(self.client.get(f'{SCHEMA_URL}{schema.id}/').status_code,
                         status.HTTP_404_NOT_FOUND)
        self.assertEqual(self.client.patch(
            f'{SCHEMA_URL}{schema.id}/', {'name': 'hijack'}, format='json').status_code,
            status.HTTP_404_NOT_FOUND)

    def test_threshold_change_resorts_existing_rows(self):
        schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS, confidence_threshold=0.9,
        )
        row = ExtractedRow.objects.create(
            schema=schema, document_name='a.pdf', confidence=0.85, data={},
        )
        row.apply_threshold()
        row.save(update_fields=['status'])
        self.assertEqual(row.status, 'needs_review')

        self.client.patch(f'{SCHEMA_URL}{schema.id}/',
                          {'confidence_threshold': 0.8}, format='json')
        row.refresh_from_db()
        self.assertEqual(row.status, 'accepted')

    def test_threshold_change_never_touches_a_human_decision(self):
        schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS, confidence_threshold=0.9,
        )
        row = ExtractedRow.objects.create(
            schema=schema, document_name='a.pdf', confidence=0.3, data={},
            status='rejected', reviewed_by=self.user,
        )
        self.client.patch(f'{SCHEMA_URL}{schema.id}/',
                          {'confidence_threshold': 0.1}, format='json')
        row.refresh_from_db()
        self.assertEqual(row.status, 'rejected')


class ExtractionRowTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS, confidence_threshold=0.8,
        )
        self.rows_url = f'{SCHEMA_URL}{self.schema.id}/rows/'

    def _row(self, conf=0.99, **kw):
        row = ExtractedRow.objects.create(
            schema=self.schema, document_name='a.pdf', confidence=conf, data={}, **kw,
        )
        row.apply_threshold()
        row.save(update_fields=['status'])
        return row

    def test_rows_post_applies_threshold(self):
        low = dict(self._row(conf=0.4).__dict__)
        payload = {'document_name': 'b.pdf', 'data': {'vendor': 'X', 'total': '1'},
                   'confidence': 0.4, 'field_confidence': {'total': 0.4}}
        response = self.client.post(self.rows_url, payload, format='json')
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['status'], 'needs_review')

        payload['confidence'] = 0.99
        response = self.client.post(self.rows_url, payload, format='json')
        self.assertEqual(response.data['status'], 'accepted')

    def test_rows_list_filters_by_status_and_paginates(self):
        self._row(conf=0.4)
        self._row(conf=0.99)
        response = self.client.get(self.rows_url, {'status': 'needs_review'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['count'], 1)
        self.assertEqual(response.data['results'][0]['status'], 'needs_review')

    def test_review_accepts_and_records_the_actor(self):
        row = self._row(conf=0.4)
        response = self.client.post(f'{ROWS_URL}{row.id}/review/', {}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'reviewed')
        self.assertEqual(response.data['corrected'], False)
        row.refresh_from_db()
        self.assertEqual(row.reviewed_by, self.user)
        self.assertIsNotNone(row.reviewed_at)

    def test_review_can_correct_values_first(self):
        row = ExtractedRow.objects.create(
            schema=self.schema, document_name='a.pdf', confidence=0.4,
            data={'vendor': 'WRONG'}, field_confidence={'vendor': 0.4},
        )
        row.apply_threshold()
        row.save(update_fields=['status'])
        response = self.client.post(f'{ROWS_URL}{row.id}/review/',
                                    {'data': {'vendor': 'RIGHT'}}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['corrected'], True)
        self.assertEqual(response.data['data']['vendor'], 'RIGHT')

    def test_review_rejects_unknown_fields(self):
        row = self._row(conf=0.4)
        response = self.client.post(f'{ROWS_URL}{row.id}/review/',
                                    {'data': {'not_a_field': 1}}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        row.refresh_from_db()
        self.assertEqual(row.status, 'needs_review')

    def test_review_rejects_the_row(self):
        row = self._row(conf=0.4)
        response = self.client.post(f'{ROWS_URL}{row.id}/review/', {'reject': True},
                                    format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['status'], 'rejected')
        self.assertIsNotNone(response.data['reviewed_at'])

    def test_review_of_another_users_row_is_404(self):
        other = User.objects.create_user(username='stranger', password='pw')
        foreign = ExtractionSchema.objects.create(user=other, name='Theirs', fields=FIELDS)
        row = ExtractedRow.objects.create(schema=foreign, document_name='x', data={})
        self.assertEqual(
            self.client.post(f'{ROWS_URL}{row.id}/review/', {}, format='json').status_code,
            status.HTTP_404_NOT_FOUND)
