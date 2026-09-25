"""
Phase F: every file type previews — or says plainly why it cannot.

What these pin, one fixture file per type:
* OpenDocument, RTF and email extract searchable text (or a listing);
* a TIFF converts to a PNG (and caches it); a zip lists entries and never
  serves their bytes, capped with `truncated` when cut;
* a legacy `.doc` is typed `doc_legacy` and never reaches the new-format
  code — neither python-docx nor the extractor reads OLE2 as a zip;
* `retype_documents` repairs rows the old vocabulary mistyped.
"""
from __future__ import annotations

import io
import os
import shutil
import tempfile
import zipfile

from django.contrib.auth import get_user_model
from django.core.files.base import ContentFile
from django.core.management import call_command
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from inference import previews, vfs
from inference.models import Document
from inference.utils import (
    extract_eml_text, extract_opendocument_text, extract_rtf_text,
    normalize_file_type,
)

User = get_user_model()

_MEDIA = tempfile.mkdtemp(prefix='previews-' )

_TEXT = 'urn:oasis:names:tc:opendocument:xmlns:text:1.0'
_TABLE = 'urn:oasis:names:tc:opendocument:xmlns:table:1.0'
_OFFICE = 'urn:oasis:names:tc:opendocument:xmlns:office:1.0'


def _odf(kind: str, body: str) -> bytes:
    mimetypes = {'odt': 'application/vnd.oasis.opendocument.text',
                 'ods': 'application/vnd.oasis.opendocument.spreadsheet',
                 'odp': 'application/vnd.oasis.opendocument.presentation'}
    content = (
        '<?xml version="1.0"?>'
        f'<office:document-content xmlns:office="{_OFFICE}" '
        f'xmlns:text="{_TEXT}" xmlns:table="{_TABLE}">'
        f'<office:body>{body}</office:body></office:document-content>')
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        zf.writestr('mimetype', mimetypes[kind])
        zf.writestr('content.xml', content)
    return buf.getvalue()


def _odt_bytes() -> bytes:
    return _odf('odt', '<office:text>'
                '<text:p>Hello <text:span text:style-name="T1">world</text:span></text:p>'
                '<table:table><table:table-row>'
                '<table:table-cell><text:p>a</text:p></table:table-cell>'
                '<table:table-cell><text:p>b</text:p></table:table-cell>'
                '</table:table-row></table:table></office:text>')


def _ods_bytes() -> bytes:
    return _odf('ods', '<office:spreadsheet><table:table>'
                '<table:table-row>'
                '<table:table-cell><text:p>x</text:p></table:table-cell>'
                '<table:table-cell table:number-columns-repeated="2"><text:p>y</text:p></table:table-cell>'
                '</table:table-row></table:table></office:spreadsheet>')


def _odp_bytes() -> bytes:
    return _odf('odp', '<office:presentation>'
                '<draw:page xmlns:draw="urn:oasis:names:tc:opendocument:xmlns:drawing:1.0">'
                '<text:p>Slide one</text:p></draw:page></office:presentation>')


_RTF = (r'{\rtf1\ansi{\fonttbl\f0 Arial;}\f0\fs24 Hello\b bold\par '
        r'New {\i line} here.\par}')


_EML = ('From: boss@example.com\r\nTo: me@example.com\r\n'
        'Subject: Q3 numbers\r\nDate: Thu, 25 Sep 2026 10:00:00 +0530\r\n'
        'Content-Type: multipart/mixed; boundary="b"\r\n\r\n'
        '--b\r\nContent-Type: text/plain\r\n\r\nSee the attached sheet.\r\n'
        '--b\r\nContent-Type: application/octet-stream; name="q3.xlsx"\r\n'
        'Content-Disposition: attachment; filename="q3.xlsx"\r\n\r\n'
        'bytesbytes\r\n--b--\r\n')


def _tiff_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new('RGB', (64, 32), (10, 200, 90)).save(buf, format='TIFF')
    return buf.getvalue()


def _zip_bytes(names) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as zf:
        for name in names:
            zf.writestr(name, b'x' * 10)
    return buf.getvalue()


class ExtractorTests(TestCase):
    def _file(self, data: bytes):
        tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.bin')
        tmp.write(data)
        tmp.close()
        self.addCleanup(lambda: os.unlink(tmp.name) if os.path.exists(tmp.name) else None)
        return tmp.name

    def test_odt_paragraphs_tables_and_spans(self):
        text = extract_opendocument_text(self._file(_odt_bytes()), 'odt')
        self.assertIn('Hello world', text)
        self.assertIn('a | b', text)

    def test_ods_rows_with_repeats(self):
        text = extract_opendocument_text(self._file(_ods_bytes()), 'ods')
        self.assertIn('x | y | y', text)

    def test_odp_slide_text(self):
        text = extract_opendocument_text(self._file(_odp_bytes()), 'odp')
        self.assertIn('Slide one', text)

    def test_rtf_words_without_control_words(self):
        text = extract_rtf_text(self._file(_RTF.encode('latin-1')))
        self.assertIn('Hello', text)
        self.assertIn('bold', text)
        self.assertIn('New line here.', text)
        self.assertNotIn('fonttbl', text)
        self.assertNotIn('\\par', text)

    def test_eml_headers_body_and_attachments(self):
        text = extract_eml_text(self._file(_EML.encode('utf-8')))
        self.assertIn('Subject: Q3 numbers', text)
        self.assertIn('See the attached sheet.', text)
        self.assertIn('Attachments: q3.xlsx', text)

    def test_legacy_office_never_reaches_new_format_code(self):
        self.assertEqual(normalize_file_type('old.doc'), 'doc_legacy')
        self.assertEqual(normalize_file_type('old.xls'), 'xls_legacy')
        self.assertEqual(normalize_file_type('old.ppt'), 'ppt_legacy')
        # OLE2 bytes are not a zip: the docx reader answers '' rather than
        # noise, and the dispatcher files them away from every new reader.
        ole2 = self._file(b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 512)
        from inference.utils import extract_docx_text, extract_text_from_file

        self.assertEqual(extract_docx_text(ole2), '')
        self.assertEqual(extract_text_from_file(ole2, 'doc_legacy'), '')


@override_settings(MEDIA_ROOT=_MEDIA)
class PreviewRouteTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(_MEDIA, ignore_errors=True)

    def setUp(self):
        self.owner = User.objects.create_user('owner', 'o@example.com', 'pw')
        self.other = User.objects.create_user('other', 't@example.com', 'pw')
        self.me = APIClient()
        self.me.force_authenticate(self.owner)
        self.them = APIClient()
        self.them.force_authenticate(self.other)

    def store(self, name, file_type, data: bytes):
        doc = Document(user=self.owner, name=name, file_type=file_type,
                       file_size=len(data), content_text='', status='stored', metadata={})
        doc.file.save(name, ContentFile(data), save=False)
        doc.save()
        return doc

    def test_a_tiff_converts_to_png_and_caches(self):
        doc = self.store('scan.tiff', 'image', _tiff_bytes())
        resp = self.me.get(f'/api/inference/documents/{doc.id}/preview-image/')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp['Content-Type'], 'image/png')
        first = b''.join(resp.streaming_content)
        self.assertTrue(first.startswith(b'\x89PNG'))
        # Cached: the row remembers the conversion.
        self.assertIn('preview_image', Document.objects.get(id=doc.id).metadata)
        second = b''.join(self.me.get(f'/api/inference/documents/{doc.id}/preview-image/').streaming_content)
        self.assertEqual(first, second)

    def test_a_png_needs_no_conversion(self):
        from PIL import Image

        buf = io.BytesIO()
        Image.new('RGB', (4, 4)).save(buf, format='PNG')
        doc = self.store('x.png', 'image', buf.getvalue())
        resp = self.me.get(f'/api/inference/documents/{doc.id}/preview-image/')
        self.assertEqual(resp.status_code, 400)

    def test_a_zip_lists_but_never_serves(self):
        doc = self.store('a.zip', 'zip', _zip_bytes(['b.txt', 'c/d.csv']))
        resp = self.me.get(f'/api/inference/documents/{doc.id}/archive/')
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual(body['count'], 2)
        self.assertFalse(body['truncated'])
        names = [e['name'] for e in body['entries']]
        self.assertIn('b.txt', names)
        # Names, sizes and dates — never bytes.
        self.assertNotIn('content', body)
        self.assertTrue(all(set(e) == {'name', 'size', 'modified'} for e in body['entries']))

    def test_a_big_zip_says_it_is_capped(self):
        doc = self.store('big.zip', 'zip', _zip_bytes([f'f{n}.txt' for n in range(510)]))
        body = self.me.get(f'/api/inference/documents/{doc.id}/archive/').json()
        self.assertTrue(body['truncated'])
        self.assertEqual(len(body['entries']), 500)
        self.assertEqual(body['count'], 510)

    def test_another_user_gets_no_preview(self):
        doc = self.store('scan.tiff', 'image', _tiff_bytes())
        self.assertEqual(
            self.them.get(f'/api/inference/documents/{doc.id}/preview-image/').status_code, 404)
        archive = self.store('a.zip', 'zip', _zip_bytes(['x']))
        self.assertEqual(
            self.them.get(f'/api/inference/documents/{archive.id}/archive/').status_code, 404)


class RetypeTests(TestCase):
    def test_retype_repairs_and_dry_run_does_not(self):
        user = User.objects.create_user('owner', 'o@example.com', 'pw')
        old = Document.objects.create(user=user, name='old.doc', file_type='docx',
                                      file_size=1, status='stored')
        call_command('retype_documents', '--dry-run')
        self.assertEqual(Document.objects.get(id=old.id).file_type, 'docx')
        call_command('retype_documents')
        self.assertEqual(Document.objects.get(id=old.id).file_type, 'doc_legacy')
        # A correct row is untouched, and so is an `other` row whose extension
        # still says nothing.
        kept = Document.objects.create(user=user, name='n.docx', file_type='docx',
                                       file_size=1, status='stored')
        other = Document.objects.create(user=user, name='blob.bin', file_type='other',
                                        file_size=1, status='stored')
        call_command('retype_documents')
        self.assertEqual(Document.objects.get(id=kept.id).file_type, 'docx')
        self.assertEqual(Document.objects.get(id=other.id).file_type, 'other')
