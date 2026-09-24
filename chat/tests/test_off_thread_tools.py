"""
Phase 3 (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): slow tools must not sit on the
run's thread.

A 60 s download on the run's single thread blocks that run's ORM calls and
serialises its parallel siblings. The proof: block `download_file` inside
`_fetch` on an event, and show a `parallel=True` sibling doing ORM
(`list_files`) completing while the download is still stuck. If `_fetch` ever
moves back on-thread, the sibling queues behind it and the `wait_for` below
times out.
"""
from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from chat.tools import execute_tool
from inference import vfs

User = get_user_model()


class BlockedDownloadLetsSiblingsThrough(TestCase):
    def setUp(self):
        self._media = tempfile.mkdtemp()
        self._override = override_settings(MEDIA_ROOT=self._media)
        self._override.enable()
        self.user = User.objects.create_user('owner', 'owner@example.com', 'pw')
        self.scope = vfs.chat_scope(self.user)

    def tearDown(self):
        self._override.disable()
        shutil.rmtree(self._media, ignore_errors=True)

    async def test_parallel_sibling_completes_while_fetch_blocked(self):
        entered = threading.Event()
        gate = threading.Event()

        def stuck_fetch(url):
            entered.set()
            assert gate.wait(timeout=30), "test released the download too late"
            return (b'hello', 'text/plain')

        ctx = {'user_id': self.user.id, 'file_scope': self.scope}
        with mock.patch('chat.tools.fetch._fetch', stuck_fetch), \
             mock.patch('core.safety.net.validate_url_async',
                        return_value=(True, '')):
            download = asyncio.ensure_future(execute_tool(
                'download_file',
                {'url': 'https://example.com/n.txt', 'path': 'n.txt'}, ctx))
            try:
                await asyncio.wait_for(asyncio.to_thread(entered.wait), timeout=30)
                sibling = await asyncio.wait_for(
                    execute_tool('list_files', {}, ctx), timeout=10)
                self.assertNotIn('error', json.loads(sibling))
            finally:
                gate.set()
            out = json.loads(await asyncio.wait_for(download, timeout=30))
            self.assertEqual(out['path'], '/Chat/n.txt')
