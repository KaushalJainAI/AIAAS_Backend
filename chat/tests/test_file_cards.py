"""Files a turn wrote or edited become cards the answer can point at.

The tool result already names the document by id, and the id is the only
locator the file browser accepts, so what matters here is that the record is
one entry per file, carries what changed, and never appears for a failed call.
"""
from __future__ import annotations

import json

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.turn.agent import (
    FILE_EDIT_PREVIEW_CHARS, FILE_EDITS_KEPT, _apply_side_effects,
)
from chat.turn.events import Event


class _Sink:
    def __init__(self):
        self.events = []

    async def __call__(self, event, payload):
        self.events.append((event, json.loads(json.dumps(payload))))


def _apply(name, args, result, meta, sink):
    async_to_sync(_apply_side_effects)(name, args, json.dumps(result), meta, sink)


WRITE = {"path": "/Chat/evals/h.py", "document_id": 7, "created": True,
         "appended": False, "chars": 120}
EDIT = {"path": "/Chat/evals/h.py", "document_id": 7, "replacements": 1,
        "chars": 125, "chars_before": 120}


class FileCardTests(SimpleTestCase):
    def test_a_write_is_recorded_and_streamed(self):
        meta, sink = {}, _Sink()
        _apply("write_file", {"path": "evals/h.py", "content": "x"}, WRITE, meta, sink)

        [card] = meta["files"]
        self.assertEqual(card["document_id"], 7)
        self.assertEqual(card["name"], "h.py")
        self.assertEqual(card["path"], "/Chat/evals/h.py")
        self.assertEqual(card["action"], "created")
        self.assertEqual(sink.events, [(Event.FILES_UPDATE, {"files": meta["files"]})])

    def test_edits_to_the_same_file_update_one_card_and_keep_created(self):
        meta, sink = {}, _Sink()
        _apply("write_file", {}, WRITE, meta, sink)
        _apply("edit_file", {"old_text": "a", "new_text": "b"}, EDIT, meta, sink)

        [card] = meta["files"]
        self.assertEqual(card["action"], "created")
        self.assertEqual(card["edits"], [{"old": "a", "new": "b", "replacements": 1}])
        self.assertEqual(card["chars"], 125)

    def test_an_edit_to_an_existing_file_says_edited(self):
        meta = {}
        _apply("edit_file", {"old_text": "a", "new_text": "b"}, EDIT, meta, _Sink())
        self.assertEqual(meta["files"][0]["action"], "edited")

    def test_an_overwrite_says_updated(self):
        meta = {}
        _apply("write_file", {}, {**WRITE, "created": False}, meta, _Sink())
        self.assertEqual(meta["files"][0]["action"], "updated")

    def test_the_diff_is_capped_and_so_is_the_edit_count(self):
        meta = {}
        big = "x" * (FILE_EDIT_PREVIEW_CHARS + 50)
        for _ in range(FILE_EDITS_KEPT + 2):
            _apply("edit_file", {"old_text": big, "new_text": "y"}, EDIT, meta, _Sink())

        card = meta["files"][0]
        self.assertEqual(len(card["edits"]), FILE_EDITS_KEPT)
        self.assertEqual(card["edits_omitted"], 2)
        self.assertEqual(len(card["edits"][0]["old"]), FILE_EDIT_PREVIEW_CHARS)

    def test_a_failed_call_leaves_no_card(self):
        meta, sink = {}, _Sink()
        _apply("write_file", {}, {"error": "No such directory"}, meta, sink)
        self.assertNotIn("files", meta)
        self.assertEqual(sink.events, [])
