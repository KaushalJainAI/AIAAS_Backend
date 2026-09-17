"""
Imports in the in-process sandbox.

`__import__` was simply absent from the sandbox builtins, so every `import`
statement failed -- allow-listed modules included -- and only code that imported
nothing could run. Found by the benchmark's data-analysis suite, where
`from datetime import datetime` came back as "ImportError: __import__ not found"
and the agent told the user it could not do date arithmetic.
"""
from django.test import SimpleTestCase

from sandbox.safe_execution import CodeSandbox


class SandboxImportTests(SimpleTestCase):
    def run_code(self, code):
        return CodeSandbox().execute(code)

    def test_allow_listed_imports_work(self):
        cases = {
            'from datetime import date\nresult = (date(2025, 3, 1) - date(2024, 2, 10)).days': 385,
            'import json\nresult = json.loads("[1, 2]")': [1, 2],
            'from collections import Counter\nresult = Counter("aab")["a"]': 2,
            'import urllib.parse\nresult = urllib.parse.quote("a b")': 'a%20b',
        }
        for code, expected in cases.items():
            with self.subTest(code=code):
                outcome = self.run_code(code)
                self.assertTrue(outcome['success'], outcome.get('error'))
                self.assertEqual(outcome['result'], expected)

    def test_only_allow_listed_attributes_come_through(self):
        outcome = self.run_code('import json\nresult = json.JSONDecoder')
        self.assertFalse(outcome['success'])

    def test_anything_else_is_refused_and_says_what_is_available(self):
        outcome = self.run_code('import csv\nresult = 1')
        self.assertFalse(outcome['success'])
        self.assertIn('not available', outcome['error'])
        self.assertIn('datetime', outcome['error'])

    def test_dangerous_imports_are_still_refused(self):
        for code in ('import os', 'from subprocess import run', '__import__("os")',
                     'from . import secrets'):
            with self.subTest(code=code):
                self.assertFalse(self.run_code(code)['success'])
