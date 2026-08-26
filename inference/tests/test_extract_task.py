"""
Tests for the LLM extraction engine (`inference/extraction.py`) and its
dispatch (`POST /api/extraction/schemas/{id}/extract/`).

The LLM call goes through `llm.access.complete` (same funnel as the chat
agent — credential resolution, platform-key fallback, clamping). These tests
mock it: what is under test is the contract — prompt shape, JSON parsing,
confidence plumbing, replace semantics, and the sync/async dispatch split.
"""
import json
from unittest.mock import AsyncMock, patch

from django.contrib.auth.models import User
from rest_framework import status
from rest_framework.test import APITestCase

from llm.access import Completion
from inference.extraction import dispatch_extraction, run_extraction
from inference.models import Document, ExtractedRow, ExtractionSchema

FIELDS = [
    {'name': 'vendor', 'label': 'Vendor', 'type': 'string', 'required': True},
    {'name': 'gstin', 'label': 'GSTIN', 'type': 'string', 'required': True},
]


def reply(fields):
    return Completion(content=json.dumps({'fields': fields}))


def _patched_complete(fields_by_doc):
    """Return an AsyncMock answering with `fields_by_doc` per document."""
    async def fake(*, provider, model, prompt, system_message, user_id,
                   temperature, max_tokens, attachments=None, **kw):
        # The prompt always names the document it is extracting.
        doc_name = [ln for ln in prompt.splitlines() if ln.startswith('Document name:')][0]
        doc_name = doc_name.split(': ', 1)[1]
        return reply(fields_by_doc.get(doc_name, {}))
    return patch('inference.extraction.llm.complete', new=AsyncMock(side_effect=fake))


class ExtractionEngineTests(APITestCase):

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.client.force_authenticate(user=self.user)
        self.schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS, confidence_threshold=0.8,
        )
        self.docs = [
            Document.objects.create(user=self.user, name=f'{i}.pdf', file_type='pdf',
                                    file_size=1, content_text=f'invoice {i}', status='indexed')
            for i in range(2)
        ]

    def _run(self, fields_by_doc):
        with _patched_complete(fields_by_doc) as mock:
            stats = run_extraction([d.id for d in self.docs], self.schema.id, self.user.id)
        return stats, mock

    def test_happy_path_creates_accepted_rows_with_min_confidence(self):
        stats, mock = self._run({
            '0.pdf': {'vendor': {'value': 'Acme', 'confidence': 0.99},
                      'gstin': {'value': '27AAACA5678M1Z2', 'confidence': 0.95}},
            '1.pdf': {'vendor': {'value': 'Baxter', 'confidence': 0.99},
                      'gstin': {'value': '24AABCB9012K1Z8', 'confidence': 0.91}},
        })
        self.assertEqual(stats['processed'], 2)
        self.assertEqual(stats['created'], 2)
        self.assertEqual(stats['needs_review'], 0)

        rows = ExtractedRow.objects.filter(schema=self.schema)
        self.assertEqual(rows.count(), 2)
        row = rows.get(document=self.docs[0])
        self.assertEqual(row.status, 'accepted')
        self.assertEqual(row.data['vendor'], 'Acme')
        self.assertEqual(row.confidence, 0.95)  # min of the field confidences
        # The engine went through the chat funnel with the schema's model.
        self.assertEqual(mock.await_count, 2)
        self.assertEqual(mock.await_args.kwargs['model'], self.schema.effective_model)
        self.assertEqual(mock.await_args.kwargs['temperature'], 0)
        self.assertIsNone(mock.await_args.kwargs['attachments'])  # text doc

    def test_vision_document_is_passed_as_attachment(self):
        image = Document.objects.create(user=self.user, name='scan.png',
                                        file_type='image', file_size=1, status='indexed')
        with _patched_complete({'scan.png': {}}) as mock:
            run_extraction([image.id], self.schema.id, self.user.id)
        self.assertIsNotNone(mock.await_args.kwargs['attachments'])
        self.assertEqual(mock.await_args.kwargs['attachments'][0].id, image.id)

    def test_below_threshold_row_is_held_for_review(self):
        stats, _ = self._run({
            '0.pdf': {'vendor': {'value': 'Acme', 'confidence': 0.99},
                      'gstin': {'value': '27AAACA5678M1Z2', 'confidence': 0.41}},
        })
        self.assertEqual(stats['needs_review'], 1)
        row = ExtractedRow.objects.get(schema=self.schema)
        self.assertEqual(row.status, 'needs_review')
        self.assertEqual(row.confidence, 0.41)

    def test_unknown_field_in_reply_fails_that_document_cleanly(self):
        stats, _ = self._run({
            '0.pdf': {'vendor': {'value': 'Acme', 'confidence': 0.99},
                      'gstin': {'value': '27AAACA5678M1Z2', 'confidence': 0.95},
                      'total': {'value': '100', 'confidence': 1.0}},  # not on schema
            '1.pdf': {'vendor': {'value': 'Baxter', 'confidence': 0.99},
                      'gstin': {'value': '24AABCB9012K1Z8', 'confidence': 0.91}},
        })
        self.assertEqual(stats['processed'], 1)
        self.assertEqual(len(stats['errors']), 1)
        self.assertIn('not on the schema', stats['errors'][0]['error'])
        self.assertEqual(ExtractedRow.objects.filter(schema=self.schema).count(), 1)

    def test_non_json_reply_fails_the_document_not_the_run(self):
        with patch('inference.extraction.llm.complete',
                   new=AsyncMock(return_value=Completion(content='sorry, no JSON'))):
            stats = run_extraction([self.docs[0].id], self.schema.id, self.user.id)
        self.assertEqual(stats['processed'], 0)
        self.assertEqual(len(stats['errors']), 1)
        self.assertEqual(ExtractedRow.objects.count(), 0)

    def test_rerun_replaces_accepted_rows_but_keeps_human_decisions(self):
        self._run({
            '0.pdf': {'vendor': {'value': 'Old', 'confidence': 0.99},
                      'gstin': {'value': 'X', 'confidence': 0.99}},
        })
        decided = ExtractedRow.objects.create(
            schema=self.schema, document=self.docs[1], document_name='1.pdf',
            data={'vendor': 'DECIDED', 'gstin': 'Y'}, confidence=0.2,
            status='reviewed', reviewed_by=self.user,
        )

        stats, _ = self._run({
            '0.pdf': {'vendor': {'value': 'New', 'confidence': 0.99},
                      'gstin': {'value': 'Z', 'confidence': 0.99}},
            '1.pdf': {'vendor': {'value': 'Newer', 'confidence': 0.99},
                      'gstin': {'value': 'W', 'confidence': 0.99}},
        })
        self.assertEqual(stats['processed'], 1)
        self.assertEqual(stats['held_decided'], 1)

        replaced = ExtractedRow.objects.get(schema=self.schema, document=self.docs[0])
        self.assertEqual(replaced.data['vendor'], 'New')
        self.assertEqual(ExtractedRow.objects.filter(schema=self.schema).count(), 2)
        decided.refresh_from_db()
        self.assertEqual(decided.data['vendor'], 'DECIDED')  # untouched
        self.assertEqual(decided.status, 'reviewed')

    def test_schema_model_field_is_used(self):
        self.schema.llm_model = 'custom/model'
        self.schema.save()
        _, mock = self._run({'0.pdf': {}})
        self.assertEqual(mock.await_args.kwargs['model'], 'custom/model')

    def test_dispatch_sync_returns_stats(self):
        with _patched_complete({'0.pdf': {}}):
            result = dispatch_extraction([self.docs[0].id], self.schema.id, self.user.id)
        self.assertEqual(result['async'], False)
        self.assertIn('processed', result)


class ExtractionTriggerTests(APITestCase):
    """POST /api/extraction/schemas/{id}/extract/ — the view's dispatch."""

    def setUp(self):
        self.user = User.objects.create_user(username='owner', password='pw')
        self.other = User.objects.create_user(username='stranger', password='pw')
        self.client.force_authenticate(user=self.user)
        self.schema = ExtractionSchema.objects.create(
            user=self.user, name='Invoices', fields=FIELDS,
        )
        self.doc = Document.objects.create(user=self.user, name='a.pdf',
                                           file_type='pdf', file_size=1, status='indexed')

    def test_extract_returns_stats_synchronously(self):
        with _patched_complete({'a.pdf': {
            'vendor': {'value': 'Acme', 'confidence': 0.99},
            'gstin': {'value': '27AAACA5678M1Z2', 'confidence': 0.95},
        }}):
            response = self.client.post(f'/api/extraction/schemas/{self.schema.id}/extract/',
                                        {'document_ids': [self.doc.id]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['async'], False)
        self.assertEqual(response.data['processed'], 1)

    def test_extract_rejects_bad_bodies(self):
        for payload in [
            {'document_ids': []},
            {'document_ids': 'not-a-list'},
            {'document_ids': [1, 'x']},
            {},
        ]:
            response = self.client.post(f'/api/extraction/schemas/{self.schema.id}/extract/',
                                        payload, format='json')
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST, payload)

    def test_extract_refuses_foreign_documents(self):
        other_doc = Document.objects.create(user=self.other, name='theirs.pdf',
                                            file_type='pdf', file_size=1, status='indexed')
        response = self.client.post(f'/api/extraction/schemas/{self.schema.id}/extract/',
                                    {'document_ids': [other_doc.id]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_extract_foreign_schema_is_404(self):
        foreign = ExtractionSchema.objects.create(user=self.other, name='Theirs',
                                                  fields=FIELDS)
        response = self.client.post(f'/api/extraction/schemas/{foreign.id}/extract/',
                                    {'document_ids': [self.doc.id]}, format='json')
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
