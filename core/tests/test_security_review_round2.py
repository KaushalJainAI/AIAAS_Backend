"""
Round two of the 2026-09-25 review (N1–N8 in docs/SECURITY_REVIEW_FIX_PLAN.md).

The messaging signatures (N4) are pinned beside the other webhook tests in
`messaging/tests/test_messaging.py`.
"""
import io
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from unittest import mock

from django.core.cache import cache
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.exceptions import ValidationError
from django.test import TestCase, override_settings

from core.safety import net


class _Redirector(BaseHTTPRequestHandler):
    target = 'http://169.254.169.254/latest/meta-data/'

    def do_GET(self):  # noqa: N802
        self.send_response(302)
        self.send_header('Location', self.target)
        self.end_headers()

    def log_message(self, *args):
        pass


def _serve():
    server = HTTPServer(('127.0.0.1', 0), _Redirector)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _only_loopback_is_safe(url):
    """The real guard, except that our local test server is allowed."""
    if url.startswith('http://127.0.0.1:'):
        return True, ''
    return _real_validate(url)


_real_validate = net.validate_url


class RedirectSsrfTests(TestCase):
    """N1/N8: a public URL that redirects inward is refused at the hop."""

    def setUp(self):
        self.server = _serve()
        self.url = f'http://127.0.0.1:{self.server.server_port}/file.pdf'

    def tearDown(self):
        self.server.shutdown()

    def test_fetch_file_refuses_a_redirect_to_metadata(self):
        with mock.patch.object(net, 'validate_url', _only_loopback_is_safe):
            with self.assertRaises(net.UnsafeURLError):
                net.fetch_file(self.url, max_bytes=1024)

    def test_download_file_reports_the_refusal(self):
        from chat.tools import fetch

        with mock.patch.object(net, 'validate_url', _only_loopback_is_safe):
            with self.assertRaisesRegex(ValueError, 'redirects somewhere'):
                fetch._fetch(self.url)

    def test_an_oversized_body_is_refused_not_truncated(self):
        class Big(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header('Content-Type', 'application/pdf')
                self.end_headers()
                self.wfile.write(b'x' * 5000)

            def log_message(self, *args):
                pass

        server = HTTPServer(('127.0.0.1', 0), Big)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            with mock.patch.object(net, 'validate_url', _only_loopback_is_safe):
                with self.assertRaises(net.FetchTooLarge):
                    net.fetch_file(f'http://127.0.0.1:{server.server_port}/',
                                   max_bytes=1000)
        finally:
            server.shutdown()


class McpDiscoveryRedirectTests(TestCase):
    """N3: discovery never follows a redirect past the guard."""

    def test_the_discovery_client_does_not_follow_redirects(self):
        import inspect

        from mcp_integration import oauth

        src = inspect.getsource(oauth)
        self.assertNotIn('follow_redirects=True', src)
        self.assertIn('follow_redirects=False', src)


def _bomb(declared_bytes: int) -> bytes:
    """A small .docx whose single part inflates to `declared_bytes`."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as z:
        z.writestr('word/document.xml', b'\0' * declared_bytes)
    return buf.getvalue()


@override_settings()
class ZipBombTests(TestCase):
    """N2: an Office file that inflates past the budget is never opened."""

    def test_budget_check(self):
        from inference import utils

        with mock.patch.object(utils, 'ZIP_UNCOMPRESSED_LIMIT', 10_000):
            self.assertFalse(utils.zip_within_budget(io.BytesIO(_bomb(50_000))))
            self.assertTrue(utils.zip_within_budget(io.BytesIO(_bomb(5_000))))
            self.assertTrue(utils.zip_within_budget(io.BytesIO(b'not a zip')))

    def test_the_extractor_refuses_without_reading(self):
        from inference import utils

        with mock.patch.object(utils, 'ZIP_UNCOMPRESSED_LIMIT', 10_000), \
                mock.patch.object(utils.zipfile.ZipFile, 'read') as read:
            self.assertEqual(utils.extract_docx_text(io.BytesIO(_bomb(50_000))), '')
            read.assert_not_called()

    def test_upload_validation_refuses_a_bomb(self):
        from inference import utils

        upload = SimpleUploadedFile('report.docx', _bomb(50_000))
        with mock.patch.object(utils, 'ZIP_UNCOMPRESSED_LIMIT', 10_000):
            with self.assertRaisesRegex(ValidationError, 'expands'):
                utils.DocumentProcessor.validate_file_upload(upload)

    def test_the_budget_is_rewound_for_the_caller(self):
        from inference import utils

        source = io.BytesIO(_bomb(100))
        utils.zip_within_budget(source)
        self.assertEqual(source.tell(), 0)


class SqliteAttachTests(TestCase):
    """N7: SQLite's file-reaching statements are refused even for writes."""

    def test_attach_detach_pragma_are_refused(self):
        from datasources.sqlcheck import SqlRefused, check

        for sql in ("ATTACH DATABASE '/tmp/x.db' AS x",
                    'DETACH DATABASE x',
                    'PRAGMA query_only=OFF'):
            with self.assertRaises(SqlRefused, msg=sql):
                check(sql, write=True)


class AdminLoginThrottleTests(TestCase):
    """N6: the admin sign-in shares the API login's rate limit."""

    def setUp(self):
        cache.clear()

    def test_the_sixth_attempt_in_a_minute_is_refused(self):
        from core.views import LoginRateThrottle

        # The test settings lift every rate; restore the production one here.
        with mock.patch.object(LoginRateThrottle, 'THROTTLE_RATES', {'login': '5/minute'}):
            codes = [self.client.post('/admin/login/',
                                      {'username': 'a', 'password': 'b'}).status_code
                     for _ in range(6)]
        self.assertNotIn(429, codes[:5])
        self.assertEqual(codes[5], 429)

    def test_the_form_still_renders(self):
        self.assertEqual(self.client.get('/admin/login/').status_code, 200)
