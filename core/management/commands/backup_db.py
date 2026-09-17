"""
`manage.py backup_db` — a consistent copy of the database, kept locally and
optionally shipped off the box.

Production runs SQLite on a Docker volume on a single EC2 instance, and the
only backup instruction was a hand-run `tar` of that volume in DEPLOYMENT.md.
Two things were wrong with that. Nothing ran it, so there was no backup. And a
`tar` of a live SQLite file can capture it mid-write, producing a copy that
opens fine and is missing the last transaction — or does not open at all.

So this command:

- **uses SQLite's online backup API** (`sqlite3.Connection.backup`), which
  copies a consistent snapshot while the app keeps writing; for PostgreSQL it
  shells out to `pg_dump`, which gives the same guarantee;
- **compresses** the result, because a database that is mostly text shrinks
  several-fold and the box has little disk;
- **keeps the newest N copies** (`--keep`, default 7) and deletes the rest, so
  a nightly cron cannot fill the disk;
- **uploads to S3** when `BACKUP_S3_BUCKET` is set. A backup stored on the
  same disk as the database does not survive losing that disk, which is the
  failure that matters most on a single-instance deployment.

Run it from cron on the host, e.g. nightly:

    0 3 * * * docker exec aiaas-backend python manage.py backup_db --keep 7
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connections

PREFIX = "db-backup-"


class Command(BaseCommand):
    help = "Write a consistent, compressed database backup; optionally upload it to S3."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dir",
            default=os.environ.get("BACKUP_DIR") or str(Path(settings.BASE_DIR) / "backups"),
            help="Where local backups are written (default: $BACKUP_DIR or <BASE_DIR>/backups).",
        )
        parser.add_argument(
            "--keep", type=int, default=int(os.environ.get("BACKUP_KEEP", "7")),
            help="How many local backups to keep (default 7). 0 keeps all.",
        )
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        db = connections[options["database"]].settings_dict
        engine = db["ENGINE"]
        out_dir = Path(options["dir"])
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

        if engine.endswith("sqlite3"):
            target = out_dir / f"{PREFIX}{stamp}.sqlite3.gz"
            self._backup_sqlite(Path(db["NAME"]), target)
        elif "postgresql" in engine:
            target = out_dir / f"{PREFIX}{stamp}.sql.gz"
            self._backup_postgres(db, target)
        else:
            raise CommandError(f"Unsupported database engine: {engine}")

        size_kb = target.stat().st_size // 1024
        self.stdout.write(f"Wrote {target} ({size_kb} KB)")

        bucket = os.environ.get("BACKUP_S3_BUCKET", "").strip()
        if bucket:
            key = f"{os.environ.get('BACKUP_S3_PREFIX', 'aiaas/db').strip('/')}/{target.name}"
            self._upload(target, bucket, key)
            self.stdout.write(f"Uploaded to s3://{bucket}/{key}")

        removed = prune(out_dir, options["keep"])
        if removed:
            self.stdout.write(f"Removed {len(removed)} old backup(s)")

    # -- engines -------------------------------------------------------------

    def _backup_sqlite(self, source: Path, target: Path) -> None:
        if not source.exists():
            raise CommandError(f"SQLite database not found: {source}")
        with tempfile.TemporaryDirectory() as tmp:
            snapshot = Path(tmp) / "snapshot.sqlite3"
            src = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
            dst = sqlite3.connect(snapshot)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
            _gzip(snapshot, target)

    def _backup_postgres(self, db: dict, target: Path) -> None:
        if shutil.which("pg_dump") is None:
            raise CommandError("pg_dump is not installed in this environment")
        env = {**os.environ, "PGPASSWORD": db.get("PASSWORD") or ""}
        cmd = [
            "pg_dump", "--no-owner", "--no-privileges",
            "-h", db.get("HOST") or "localhost",
            "-p", str(db.get("PORT") or 5432),
            "-U", db.get("USER") or "postgres",
            db["NAME"],
        ]
        with gzip.open(target, "wb") as out:
            proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, check=False)
            if proc.returncode != 0:
                target.unlink(missing_ok=True)
                raise CommandError(f"pg_dump failed: {proc.stderr.decode(errors='replace')[:500]}")
            out.write(proc.stdout)

    def _upload(self, path: Path, bucket: str, key: str) -> None:
        try:
            import boto3
        except ImportError as exc:  # pragma: no cover — boto3 is in requirements
            raise CommandError("BACKUP_S3_BUCKET is set but boto3 is not installed") from exc
        boto3.client("s3").upload_file(str(path), bucket, key)


def _gzip(source: Path, target: Path) -> None:
    with open(source, "rb") as src, gzip.open(target, "wb") as dst:
        shutil.copyfileobj(src, dst)


def prune(directory: Path, keep: int) -> list[Path]:
    """Delete all but the newest `keep` backups. Only touches this command's files."""
    if keep <= 0:
        return []
    backups = sorted(directory.glob(f"{PREFIX}*.gz"), key=lambda p: p.name, reverse=True)
    stale = backups[keep:]
    for path in stale:
        path.unlink(missing_ok=True)
    return stale
