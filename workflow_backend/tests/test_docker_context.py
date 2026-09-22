"""Every Django app must survive the trip into the Docker image.

The `data` app (2026-09-21) proved why: it was a top-level package that
.dockerignore excluded as runtime data *and* that the compose volume
`backend_data:/app/data` mounted over at runtime. Python resolved the mount
dir as an empty namespace package, so Django booted healthy while every
`from data.models import ...` raised `ModuleNotFoundError` — which surfaced
as a 500 on every template install in production and green tests locally.

Two rules pin both halves: no local app package may match a `.dockerignore`
directory exclusion, and none may share a name with a compose volume target
under `/app`.
"""
import re
from pathlib import Path

from django.apps import apps
from django.conf import settings
from django.test import SimpleTestCase


def _backend_root() -> Path:
    return Path(settings.BASE_DIR)


def _dockerignored_top_dirs() -> set[str]:
    """Bare directory patterns from .dockerignore, e.g. `data/` -> `data`."""
    names = set()
    ignore = _backend_root() / '.dockerignore'
    if not ignore.exists():
        return names
    for line in ignore.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith('#') or line.startswith('!'):
            continue
        # Only the blunt top-level exclusions can swallow a whole app: a
        # pattern with no slash, glob or extension, ending in `/`.
        if '/' in line.strip('/') or '*' in line or '?' in line or '[' in line:
            continue
        if line.endswith('/'):
            names.add(line[:-1])
    return names


def _compose_app_mounts() -> set[str]:
    """Basenames of compose volume targets under /app, e.g. `/app/data`."""
    names = set()
    root = _backend_root().parent
    for compose in ('docker-compose.yml', 'docker-compose.prod.yml'):
        path = root / compose
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            for target in re.findall(r':(/app/[\w.-]+)', line):
                names.add(target.rsplit('/', 1)[-1])
    return names


def _local_configs():
    """App configs whose code lives in this repo — not contrib or site-packages."""
    root = _backend_root()
    out = []
    for config in apps.get_app_configs():
        try:
            path = Path(config.path)
        except Exception:
            continue
        if path == root or root in path.parents:
            out.append(config)
    return out


class DockerContextTests(SimpleTestCase):
    def test_no_app_is_excluded_from_the_image(self):
        ignored = _dockerignored_top_dirs()
        clashes = [c.label for c in _local_configs()
                   if c.name.split('.')[0] in ignored]
        self.assertEqual(
            clashes, [],
            f'Apps excluded from the Docker image by .dockerignore: {clashes}. '
            f'Rename the app — re-including files under an excluded dir is not possible.',
        )

    def test_no_app_is_shadowed_by_a_volume_mount(self):
        mounts = _compose_app_mounts()
        clashes = [c.label for c in _local_configs()
                   if c.name.split('.')[0] in mounts]
        self.assertEqual(
            clashes, [],
            f'Apps hidden at runtime by a compose volume over /app/<name>: {clashes}. '
            f'The mount wins and the package resolves as an empty namespace.',
        )

    def test_every_local_app_imports_its_models(self):
        # The shape the outage took: the package resolved (as a namespace)
        # while `app.models` did not. Every local app ships a models module
        # and it must import cleanly.
        for config in _local_configs():
            with self.subTest(app=config.label):
                if not (Path(config.path) / 'models.py').exists():
                    self.skipTest('no models module')
                __import__(f'{config.name}.models')
