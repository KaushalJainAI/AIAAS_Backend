"""
Binary files in the virtual filesystem (`vfs.write_binary`, `vfs.read_image`).

The properties defended here are the ones the render tools lean on to run
without asking: a render never destroys a file (a taken name is renamed, and
an explicit overwrite replaces the file in place — same id — after keeping
what it held as a version), it is confined by the same scope as every other
write, and the text it leaves behind is what keeps search and reading working
on a file whose bytes they cannot read.
"""
from __future__ import annotations

import shutil
import tempfile

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from inference import vfs
from inference.models import Document
from workflow_backend.thresholds import AGENT_FILE_BINARY_BYTES

User = get_user_model()

PPTX = b'PK\x03\x04 pretend this is a deck'
PNG = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06'
       b'\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00'
       b'\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82')


class BinaryTestCase(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.other = User.objects.create_user('other', 'other@example.com', 'pw')
        self.chat = vfs.chat_scope(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)


class WriteBinaryTests(BinaryTestCase):
    def test_bytes_are_the_file_and_text_is_the_extract(self):
        out = vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='Slide 1: Q3',
                               spec={'kind': 'deck'})
        doc = Document.objects.get(id=out['document_id'])
        self.assertEqual(doc.file_type, 'pptx')
        self.assertEqual(doc.status, 'stored')
        self.assertIsNone(doc.knowledge_base_id)
        with doc.file.open('rb') as fh:
            self.assertEqual(fh.read(), PPTX)
        self.assertEqual(doc.content_text, 'Slide 1: Q3')
        self.assertEqual(doc.metadata['spec'], {'kind': 'deck'})
        self.assertEqual(out['path'], '/Chat/q3.pptx')
        self.assertTrue(out['created'])
        self.assertEqual(out['bytes'], len(PPTX))

    def test_a_taken_name_is_renamed_never_replaced(self):
        first = vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='one')
        second = vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='two')
        self.assertEqual(second['path'], '/Chat/q3 (2).pptx')
        self.assertTrue(second['renamed'])
        # The original is untouched.
        self.assertEqual(Document.objects.get(id=first['document_id']).content_text, 'one')

    def test_overwrite_replaces_in_place_and_keeps_a_version(self):
        # In place rather than trash-and-recreate (2026-09-25): a new id pulled
        # the file out from under anyone who had it open in an app. The old
        # bytes are restorable through version history, not the recycle bin.
        from inference.models import DocumentVersion

        first = vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='one')
        second = vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='two', overwrite=True)
        self.assertEqual(second['path'], '/Chat/q3.pptx')
        self.assertTrue(second['replaced'])
        self.assertFalse(second['created'])
        self.assertEqual(second['document_id'], first['document_id'])
        doc = Document.objects.get(id=first['document_id'])
        self.assertIsNone(doc.deleted_at)
        self.assertEqual(doc.content_text, 'two')
        version = DocumentVersion.objects.get(document_id=doc.id)
        self.assertEqual(version.content_text, 'one')
        self.assertEqual(version.source, 'agent')

    def test_writes_are_confined_to_the_scope(self):
        with self.assertRaises(vfs.VfsError):
            vfs.write_binary(self.chat, '/Elsewhere/q3.pptx', PPTX)
        readonly = vfs.build_scope(self.user, vfs.READONLY)
        with self.assertRaises(vfs.VfsError):
            vfs.write_binary(readonly, '/q3.pptx', PPTX)
        self.assertFalse(Document.objects.filter(name='q3.pptx').exists())

    def test_refusals(self):
        with self.assertRaisesRegex(vfs.VfsError, 'extensions'):
            vfs.write_binary(self.chat, '/Chat/q3.txt', PPTX)
        with self.assertRaisesRegex(vfs.VfsError, 'empty'):
            vfs.write_binary(self.chat, '/Chat/q3.pptx', b'')
        with self.assertRaisesRegex(vfs.VfsError, 'limit'):
            vfs.write_binary(self.chat, '/Chat/q3.pptx', b'x' * (AGENT_FILE_BINARY_BYTES + 1))

    def test_the_extract_is_searchable_and_readable(self):
        vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='Revenue grew 38%')
        hits = vfs.find(self.chat, 'grew 38')
        self.assertEqual([h['path'] for h in hits['matches']], ['/Chat/q3.pptx'])
        read = vfs.read_file(self.chat, '/Chat/q3.pptx')
        self.assertEqual(read['binary'], 'pptx')
        self.assertIn('extracted', read['note'])

    def test_a_binary_cannot_be_edited_as_text(self):
        vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX, text='Revenue grew 38%')
        with self.assertRaisesRegex(vfs.VfsError, 'render it again'):
            vfs.edit_file(self.chat, '/Chat/q3.pptx', 'grew', 'fell')

    def test_write_file_refuses_binary_extensions(self):
        # A text body saved as report.docx downloads as a corrupt Word file.
        with self.assertRaisesRegex(vfs.VfsError, 'render_document'):
            vfs.write_file(self.chat, '/Chat/report.docx', 'hello')


class ReadImageTests(BinaryTestCase):
    def test_reads_an_image_in_scope(self):
        vfs.write_binary(self.chat, '/Chat/logo.png', PNG)
        data, shown = vfs.read_image(self.chat, '/Chat/logo.png')
        self.assertEqual(data, PNG)
        self.assertEqual(shown, '/Chat/logo.png')

    def test_refuses_what_is_not_an_image(self):
        vfs.write_binary(self.chat, '/Chat/q3.pptx', PPTX)
        with self.assertRaisesRegex(vfs.VfsError, 'not an image'):
            vfs.read_image(self.chat, '/Chat/q3.pptx')

    def test_cannot_reach_another_users_image(self):
        theirs = vfs.chat_scope(self.other)
        vfs.write_binary(theirs, '/Chat/logo.png', PNG)
        with self.assertRaisesRegex(vfs.VfsError, 'No such'):
            vfs.read_image(self.chat, '/Chat/logo.png')
