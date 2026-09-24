"""
Phase 2 (`docs/CONCURRENCY_LAG_FIX_PLAN.md`): the Postgres saver must resolve
a DSN from whatever the deploy configures.

The saver package itself is not installed outside the prod image, so what is
tested here is the resolution, not the saver: `_dsn_from_django_db` derives
the checkpointer's DSN from Django's own default database, which is what
guarantees the saver cannot land on a different host/database from the rows
that name its threads.
"""
from __future__ import annotations

from django.test import SimpleTestCase
from django.test.utils import override_settings

from chat.turn.checkpoints import _dsn_from_django_db


class DsnFromDjangoDbTests(SimpleTestCase):
    def test_sqlite_yields_no_dsn(self):
        """Dev SQLite: nothing to derive, so the explicit-DSN error stands."""
        self.assertEqual(_dsn_from_django_db(), '')

    @override_settings(DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'aiaas',
            'USER': 'postgres',
            'PASSWORD': 'p@ss:word/db',
            'HOST': 'db',
            'PORT': '5432',
        }
    })
    def test_postgres_db_yields_quoted_dsn(self):
        self.assertEqual(
            _dsn_from_django_db(),
            'postgresql://postgres:p%40ss%3Aword%2Fdb@db:5432/aiaas',
        )

    @override_settings(DATABASES={
        'default': {
            'ENGINE': 'django.db.backends.postgresql',
            'NAME': 'aiaas',
            'USER': '',
            'PASSWORD': '',
            'HOST': '',
            'PORT': '',
        }
    })
    def test_missing_host_and_auth_fall_back(self):
        self.assertEqual(
            _dsn_from_django_db(), 'postgresql://localhost:5432/aiaas',
        )

    @override_settings(DATABASES={'default': {'ENGINE': 'x', 'NAME': ''}})
    def test_no_name_yields_no_dsn(self):
        self.assertEqual(_dsn_from_django_db(), '')
