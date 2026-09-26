"""
`manage.py boot`: what the container runs before the server starts.

Pinned: the catalogue is seeded on a container's first start and skipped on a
restart of the same container (the marker), `--force-seed` overrides that, and
migrations run every time. See `core/management/commands/boot.py` for why each
second here is a second of 502s.
"""
from __future__ import annotations

import io
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase


class BootCommandTests(TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = Path(self.tmp.name) / 'seeded'
        patcher = patch('core.management.commands.boot.SEED_MARKER', self.marker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _boot(self, *args) -> tuple[str, int, int]:
        out = io.StringIO()
        with patch('populate_models.populate') as seed, \
             patch('core.management.commands.boot.call_command') as migrate:
            call_command('boot', *args, stdout=out)
        return out.getvalue(), seed.call_count, migrate.call_count

    def test_first_start_migrates_and_seeds(self):
        output, seeds, migrations = self._boot()
        self.assertEqual((seeds, migrations), (1, 1))
        self.assertTrue(self.marker.exists())
        self.assertIn('seeded in', output)

    def test_a_restart_of_the_same_container_skips_the_seed(self):
        self._boot()
        output, seeds, migrations = self._boot()
        self.assertEqual((seeds, migrations), (0, 1))
        self.assertIn('skipped', output)

    def test_force_seed_reseeds(self):
        self._boot()
        _, seeds, _ = self._boot('--force-seed')
        self.assertEqual(seeds, 1)

    def test_migrate_runs_non_interactively(self):
        with patch('populate_models.populate'), \
             patch('core.management.commands.boot.call_command') as migrate:
            call_command('boot', stdout=io.StringIO())
        migrate.assert_called_once_with('migrate', interactive=False, verbosity=1)
