"""
`manage.py backup_db` produces a backup that actually restores.

The point of a backup is the restore, so the main test opens the gzipped copy
as a database and reads a row back out of it, rather than checking that a file
of non-zero size appeared.
"""
from __future__ import annotations

import gzip
import sqlite3
import tempfile
from io import StringIO
from pathlib import Path
from unittest import mock

from django.core.management import call_command
from django.test import SimpleTestCase

from core.management.commands import backup_db


class BackupDbTests(SimpleTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

        self.source = self.tmp / "live.sqlite3"
        con = sqlite3.connect(self.source)
        con.execute("create table t (v text)")
        con.execute("insert into t values ('survives')")
        con.commit()
        con.close()

        fake = {"default": mock.Mock(settings_dict={
            "ENGINE": "django.db.backends.sqlite3", "NAME": str(self.source),
        })}
        patcher = mock.patch.object(backup_db, "connections", fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, **kwargs) -> str:
        out = StringIO()
        call_command("backup_db", dir=str(self.tmp / "backups"), stdout=out, **kwargs)
        return out.getvalue()

    def test_the_backup_restores_and_contains_the_data(self):
        self._run(keep=7)
        [backup] = list((self.tmp / "backups").glob("db-backup-*.sqlite3.gz"))

        restored = self.tmp / "restored.sqlite3"
        with gzip.open(backup, "rb") as src, open(restored, "wb") as dst:
            dst.write(src.read())
        con = sqlite3.connect(restored)
        try:
            self.assertEqual(con.execute("select v from t").fetchall(), [("survives",)])
        finally:
            con.close()

    def test_prune_keeps_only_the_newest(self):
        d = self.tmp / "backups"
        d.mkdir()
        for stamp in ("20260101T000000Z", "20260102T000000Z", "20260103T000000Z"):
            (d / f"db-backup-{stamp}.sqlite3.gz").write_bytes(b"x")
        (d / "unrelated.gz").write_bytes(b"x")

        removed = backup_db.prune(d, keep=2)

        self.assertEqual([p.name for p in removed], ["db-backup-20260101T000000Z.sqlite3.gz"])
        self.assertTrue((d / "unrelated.gz").exists(), "prune must not touch files it did not write")

    def test_uploads_when_a_bucket_is_configured(self):
        with mock.patch.dict("os.environ", {"BACKUP_S3_BUCKET": "my-bucket"}), \
                mock.patch.object(backup_db.Command, "_upload") as upload:
            output = self._run(keep=7)
        upload.assert_called_once()
        _, bucket, key = upload.call_args.args
        self.assertEqual(bucket, "my-bucket")
        self.assertTrue(key.startswith("aiaas/db/db-backup-"))
        self.assertIn("Uploaded to s3://my-bucket/", output)

    def test_does_not_upload_without_a_bucket(self):
        with mock.patch.dict("os.environ", {"BACKUP_S3_BUCKET": ""}), \
                mock.patch.object(backup_db.Command, "_upload") as upload:
            self._run(keep=7)
        upload.assert_not_called()
