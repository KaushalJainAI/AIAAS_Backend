"""
Prepare the database before the server starts. The image's `CMD` runs this,
then `exec`s daphne.

    python manage.py boot
    python manage.py boot --force-seed

Every second here is a second of 502s: on a deploy, and on every crash
restart. It used to be three separate processes — `collectstatic --clear`,
`migrate`, then `populate_models` through `manage.py shell` — and each one
re-imported all of Django before doing anything. Measured on 2026-09-26 that
was 44 s from container start to a listening server, of which ~36 s was
repeated setup (`learning/16_deploy_downtime_and_background_work.md`).

Now:

* **Static files are collected at image build time** (the Dockerfile), since
  they are part of the image and cannot change between restarts of it.
* **One process** runs `migrate` and the catalogue seed, so Django loads once.
* **The catalogue is seeded once per container**, not once per start. A deploy
  creates a new container, so it re-seeds (the image may carry new model rows,
  prices or effort rungs, which is why the seed runs at boot at all). A crash
  restart reuses the container, so it goes straight to serving. The marker
  lives in the container's own filesystem (`/tmp`) precisely so it dies with
  the container; after restoring a database by hand, run `--force-seed`.

`migrate` still runs on every start. With nothing pending it takes about a
second, and a server must never start against a schema it does not expect.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import BaseCommand
from django.utils import timezone

#: Present once this container has seeded the catalogue.
SEED_MARKER = Path(os.environ.get('BOOT_SEED_MARKER', '/tmp/aiaas-catalogue-seeded'))


class Command(BaseCommand):
    help = 'Migrate, seed the model catalogue once per container, then exit.'

    def add_arguments(self, parser):
        parser.add_argument(
            '--force-seed', action='store_true',
            help='Re-seed the model catalogue even if this container already did.',
        )

    def handle(self, *args, **options):
        started = time.monotonic()
        call_command('migrate', interactive=False, verbosity=1)
        migrated = time.monotonic()

        if options['force_seed'] or not SEED_MARKER.exists():
            import populate_models

            populate_models.populate()
            try:
                SEED_MARKER.write_text(timezone.now().isoformat())
            except OSError:
                # A read-only filesystem only costs a re-seed next start.
                pass
            seeded = f'seeded in {time.monotonic() - migrated:.1f}s'
        else:
            seeded = 'catalogue already seeded by this container, skipped'

        self.stdout.write(
            f'[boot] migrate {migrated - started:.1f}s, {seeded}; '
            f'total {time.monotonic() - started:.1f}s')
