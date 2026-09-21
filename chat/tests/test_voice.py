"""
Voice + documents (P9): transcription, speech, OCR, e-sign tools.

Pinned: engines `none` means refused-with-a-reason, never offered-then-
silent; speech caps refuse rather than truncate; every non-token spend lands
in the ledger estimated; an image with no text is referred to the witness,
never guessed at; signer addresses are validated, never trusted.
"""
from __future__ import annotations

import io
import json
import shutil
import tempfile
from unittest import mock

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from chat.tools import execute_tool
from inference import vfs
from logs.models import CostEntry

User = get_user_model()


class ScopedVoiceTestCase(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    def call(self, name, args, **extra):
        ctx = {'user_id': self.user.id, 'file_scope': self.scope, **extra}
        return json.loads(async_to_sync(execute_tool)(name, args, ctx))

    def write(self, path, data: bytes, text: str = ''):
        from inference.vfs import write_binary

        return write_binary(self.scope, path, data, text=text)


def _pdf(text: str) -> bytes:
    from reportlab.pdfgen.canvas import Canvas

    buf = io.BytesIO()
    canvas = Canvas(buf)
    canvas.drawString(72, 720, text)
    canvas.showPage()
    canvas.save()
    return buf.getvalue()


class TranscribeTests(ScopedVoiceTestCase):
    def test_a_transcript_is_saved_beside_the_audio(self):
        self.write('/Chat/meet.mp3', b'\x00' * 2_000_000, text='')
        with mock.patch('voice.stt.transcribe', autospec=True) as fake:
            async def _t(*a, **k):
                return {'text': 'hello', 'segments': [
                    {'start': '00:01', 'speaker': 'Asha', 'text': 'hello'}],
                    'duration_s': 120, 'language': 'en'}
            fake.side_effect = _t
            out = self.call('transcribe_audio', {'path': '/Chat/meet.mp3'})
        self.assertEqual(out['transcript_path'], '/Chat/meet.md')
        body = vfs.read_file(self.scope, '/Chat/meet.md')['content']
        self.assertIn('Asha', body)
        row = CostEntry.objects.get(user=self.user, kind='transcription')
        self.assertTrue(row.estimated)
        self.assertGreater(row.amount_inr, 0)

    def test_no_engine_is_a_reason_not_a_silence(self):
        self.write('/Chat/meet.mp3', b'\x00' * 100, text='')
        out = self.call('transcribe_audio', {'path': '/Chat/meet.mp3'})
        self.assertIn('No transcription engine', out['error'])

    def test_an_empty_transcript_is_an_error(self):
        self.write('/Chat/meet.mp3', b'\x00' * 100, text='')
        with mock.patch('voice.stt.transcribe', autospec=True) as fake:
            async def _t(*a, **k):
                return {'text': '', 'segments': [], 'duration_s': 0, 'language': ''}
            fake.side_effect = _t
            out = self.call('transcribe_audio', {'path': '/Chat/meet.mp3'})
        self.assertIn('empty', out['error'])


class SpeechTests(ScopedVoiceTestCase):
    def test_speech_is_saved_and_spends(self):
        with mock.patch('voice.tts.synthesize', autospec=True) as fake:
            async def _s(*a, **k):
                return b'ID3audio', 'mp3'
            fake.side_effect = _s
            out = self.call('text_to_speech', {'text': 'Namaste'})
        self.assertEqual(out['path'], '/Chat/audio/speech.mp3')
        row = CostEntry.objects.get(user=self.user, kind='transcription')
        self.assertTrue(row.estimated)

    def test_a_long_passage_is_refused_not_cut(self):
        out = self.call('text_to_speech', {'text': 'x' * 5001})
        self.assertIn('5,000', out['error'])

    def test_speech_is_sensitive(self):
        from chat.tools.registry import get

        self.assertTrue(get('text_to_speech').sensitive)
        self.assertEqual(get('text_to_speech').effect, 'irreversible')


class OcrTests(ScopedVoiceTestCase):
    def test_a_scanned_pdf_reads_by_page(self):
        self.write('/Chat/bill.pdf', _pdf('Invoice total 120'), text='')
        out = self.call('ocr_document', {'path': '/Chat/bill.pdf'})
        self.assertEqual(out['pages'], 1)
        self.assertIn('120', out['text'])

    def test_table_mode_returns_rows(self):
        self.write('/Chat/bill.pdf', _pdf('apple 10'), text='')
        out = self.call('ocr_document', {'path': '/Chat/bill.pdf', 'as': 'table'})
        self.assertTrue(out['rows'])

    def test_fields_mode_needs_a_schema_the_user_owns(self):
        self.write('/Chat/bill.pdf', _pdf('x'), text='')
        out = self.call('ocr_document', {
            'path': '/Chat/bill.pdf', 'as': 'fields', 'schema_id': 424242})
        self.assertIn('schema', out['error'])

    def test_fields_mode_runs_the_engine(self):
        from inference.models import ExtractionSchema

        self.write('/Chat/bill.pdf', _pdf('x'), text='')
        schema = ExtractionSchema.objects.create(
            user=self.user, name='Bills', fields=[{'name': 'total'}])
        with mock.patch('inference.extraction.run_extraction') as run:
            run.return_value = {'created': 1, 'needs_review': 0}
            out = self.call('ocr_document', {
                'path': '/Chat/bill.pdf', 'as': 'fields', 'schema_id': schema.id})
        self.assertIn('Bills', out['schema'])

    def test_an_image_with_no_text_names_the_witness(self):
        self.write('/Chat/photo.jpg', b'\xff\xd8fake', text='')
        out = self.call('ocr_document', {'path': '/Chat/photo.jpg'})
        self.assertIn('ask_vision', out['error'])

    def test_ocr_is_read_only(self):
        from chat.tools.registry import get

        self.assertEqual(get('ocr_document').effect, 'read')


class EsignToolTests(ScopedVoiceTestCase):
    def _doc(self):
        return self.write('/Chat/offer.pdf', _pdf('Offer letter'), text='Offer letter')

    def test_sending_needs_a_provider(self):
        self._doc()
        out = self.call('request_signature', {
            'path': '/Chat/offer.pdf',
            'signers': [{'name': 'Asha', 'email': 'asha@example.com'}]})
        self.assertIn('No e-signature provider', out['error'])

    def test_bad_signers_are_refused(self):
        self._doc()
        out = self.call('request_signature', {
            'path': '/Chat/offer.pdf', 'signers': [{'name': 'Asha'}]})
        self.assertIn('signer', out['error'])

    def test_send_records_and_status_reads(self):
        from esign.models import SignatureRequest

        self._doc()
        with mock.patch('esign.provider.send', autospec=True) as send:
            async def _s(**k):
                return 'prov-1'
            send.side_effect = _s
            with mock.patch('esign.provider.esign_available', return_value=True):
                out = self.call('request_signature', {
                    'path': '/Chat/offer.pdf',
                    'signers': [{'name': 'Asha', 'email': 'asha@example.com'}]})
        self.assertEqual(out['status'], 'sent')
        row = SignatureRequest.objects.get(id=out['request_id'])
        self.assertEqual(row.provider_request_id, 'prov-1')
        status = self.call('signature_status', {'request_id': row.id})
        self.assertEqual(status['status'], 'sent')

    def test_status_never_leaks_another_users_row(self):
        other = User.objects.create_user('other', 'o@example.com', 'pw')
        from esign.models import SignatureRequest

        row = SignatureRequest.objects.create(
            user=other, path='/x.pdf', signers=[], status='sent')
        out = self.call('signature_status', {'request_id': row.id})
        self.assertIn('belongs to this user', out['error'])
