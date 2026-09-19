"""The sandbox file bridge: inputs in, declared outputs back into the scope.

`execute_python` is read-only so it survives `plan` mode; this is the
reversible half over the same engine. Without a `FileScope` the file params
are refused, not ignored — an advertised tool that cannot run is worse than
one never offered. Outputs land in the scope's own write folder and never
overwrite: a taken name becomes `name (2).ext`, the same rule the office
tools follow.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from chat.tools import sandbox as sandbox_tools
from inference import vfs
from inference.models import Document


@override_settings(SANDBOX_ENGINE="inprocess")
class SandboxFileBridgeTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user(
            username="bridger", email="b@example.test", password="x"
        )
        self.scope = vfs.chat_scope(self.user)

    def _run(self, args, scope="default"):
        context = {
            "user_id": self.user.id,
            "session_id": "sess-1",
            "file_scope": self.scope if scope == "default" else scope,
        }
        return async_to_sync(sandbox_tools.run_python_on_files)(args, context)

    def _read(self, raw: str) -> dict:
        try:
            return json.loads(raw)
        except ValueError:
            return {"_raw": raw}

    def test_csv_in_summary_out(self):
        vfs.write_file(self.scope, "/Chat/sales.csv", "region,revenue\nN,100\nS,150\n")
        raw = self._run({
            "code": (
                "import csv\n"
                "with open('sales.csv') as f:\n"
                "    rows = list(csv.DictReader(f))\n"
                "total = sum(int(r['revenue']) for r in rows)\n"
                "with open('summary.csv', 'w') as f:\n"
                "    f.write(f'total,{total}\\n')\n"
                "result = total"
            ),
            "inputs": ["/Chat/sales.csv"],
            "outputs": ["summary.csv"],
        })
        out = self._read(raw)
        self.assertEqual(out.get("result"), 250)
        self.assertEqual(len(out.get("files") or []), 1)
        saved = out["files"][0]
        self.assertEqual(saved["path"], "/Chat/summary.csv")
        doc = Document.objects.get(id=saved["document_id"])
        self.assertIn("250", doc.content_text)

    def test_without_a_scope_files_are_refused(self):
        raw = self._run(
            {"code": "result = 1", "inputs": ["/Chat/sales.csv"], "outputs": ["o.csv"]},
            scope=None,
        )
        self.assertIn("no file workspace", self._read(raw).get("error", ""))

    def test_a_missing_input_is_an_answer_not_a_traceback(self):
        raw = self._run({"code": "result = 1", "inputs": ["/Chat/nope.csv"]})
        self.assertIn("No such file", self._read(raw).get("error", ""))

    def test_outputs_land_in_the_write_folder_and_never_overwrite(self):
        vfs.write_file(self.scope, "/Chat/out.csv", "old")
        raw = self._run({
            "code": "with open('out.csv', 'w') as f:\n    f.write('new')",
            "outputs": ["out.csv"],
        })
        out = self._read(raw)
        saved = out["files"][0]
        self.assertEqual(saved["path"], "/Chat/out (2).csv")
        self.assertEqual(Document.objects.get(name="out.csv").content_text, "old")

    def test_an_undeclared_file_is_reported_not_kept(self):
        raw = self._run({
            "code": "with open('extra.log', 'w') as f:\n    f.write('x')",
            "outputs": ["wanted.csv"],
        })
        out = self._read(raw)
        self.assertIn("extra.log", out.get("unsaved") or [])
        self.assertFalse(Document.objects.filter(name="extra.log").exists())

    def test_a_bad_output_name_is_refused(self):
        raw = self._run({"code": "result = 1", "outputs": ["../evil"]})
        self.assertIn("plain file name", self._read(raw).get("error", ""))

    def test_execute_python_stays_without_files(self):
        # The read-only tool must not grow file params: it is what `plan`
        # mode offers, and a file-writing `plan` tool breaks that mode's
        # whole promise.
        from chat.tools.registry import get as registered

        params = registered("execute_python").schema["function"]["parameters"]["properties"]
        self.assertNotIn("inputs", params)
        self.assertNotIn("outputs", params)
        entry = registered("run_python_on_files")
        self.assertEqual(entry.requires, "files")
        self.assertEqual(entry.effect, "reversible")
