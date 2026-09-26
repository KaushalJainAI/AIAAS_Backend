"""
The import contracts in `Backend/.importlinter` hold.

Run as a test so CI enforces them with no extra step: a change that makes a
lower layer import the agent runtime, the chat engine or another app's views
fails here, naming the import, instead of adding one more hidden cycle. To see
the full report locally, run `lint-imports` from `Backend/`.
"""
from __future__ import annotations

import contextlib
import io
import os
from pathlib import Path

from django.test import SimpleTestCase

BACKEND = Path(__file__).resolve().parents[2]


class ImportContractTests(SimpleTestCase):
    def test_the_layers_hold(self):
        from importlinter.cli import lint_imports

        report = io.StringIO()
        previous = os.getcwd()
        os.chdir(BACKEND)
        try:
            with contextlib.redirect_stdout(report):
                code = lint_imports(config_filename=str(BACKEND / '.importlinter'),
                                    no_cache=True, no_logo=True)
        finally:
            os.chdir(previous)
        self.assertEqual(code, 0, 'An import contract is broken:\n' + report.getvalue())
