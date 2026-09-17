"""
The native Google connector tools, against a fake wire.

Only `client._send` (the one network call) and, where the credential is not
the subject, `client._access_token` are replaced. Everything between — URL
building, retry, error classification, MIME decoding, export selection — runs
for real, because that is where these tools can be wrong in ways a mocked
`get_json` would hide.
"""
from __future__ import annotations

import base64
import email
import json
from unittest.mock import AsyncMock, patch

import httpx
from asgiref.sync import async_to_sync
from django.test import SimpleTestCase

from chat.tools.google import calendar, client, drive, gmail

CTX = {"user_id": None, "session_id": "s", "turn_id": "t"}


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _json(status: int, body) -> httpx.Response:
    return httpx.Response(status, json=body)


class Wire:
    """Routes (method, url fragment) to canned responses, and records calls."""

    def __init__(self, routes):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    async def __call__(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        for route_method, fragment, response in self.routes:
            if route_method == method and fragment in url:
                if isinstance(response, list):
                    return response.pop(0)
                return response
        raise AssertionError(f"unexpected {method} {url}")


def run(tool, args, wire, token="tok"):
    with patch.object(client, "_send", new=wire), \
         patch.object(client, "_access_token", new=AsyncMock(return_value=token)) as tok, \
         patch.object(client.asyncio, "sleep", new=AsyncMock()):
        out = async_to_sync(tool)(args, CTX)
    return out, tok


def parsed(out: str) -> dict:
    return json.loads(out)


class ClientFailureTests(SimpleTestCase):
    """The codes a model acts on. Each one means a different next step."""

    def test_a_401_refreshes_once_and_retries(self):
        wire = Wire([("GET", "/labels", [
            _json(401, {"error": {"message": "Invalid Credentials"}}),
            _json(200, {"labels": []}),
        ])])
        out, tok = run(gmail.gmail_list_labels, {}, wire)
        self.assertEqual(parsed(out)["labels"], [])
        self.assertEqual(tok.await_args_list[-1].kwargs, {"force_refresh": True})
        self.assertEqual(len(wire.calls), 2)

    def test_a_second_401_is_reported_not_retried_forever(self):
        wire = Wire([("GET", "/labels", _json(401, {"error": {"message": "nope"}}))])
        out, _ = run(gmail.gmail_list_labels, {}, wire)
        self.assertEqual(parsed(out)["code"], "credential_invalid")
        self.assertEqual(len(wire.calls), 2)

    def test_insufficient_scope_says_reconnect(self):
        wire = Wire([("GET", "/labels", _json(403, {"error": {
            "message": "Request had insufficient authentication scopes.",
            "details": [{"reason": "ACCESS_TOKEN_SCOPE_INSUFFICIENT"}],
        }}))])
        out, _ = run(gmail.gmail_list_labels, {}, wire)
        self.assertEqual(parsed(out)["code"], "scope_missing")
        self.assertIn("Connections", parsed(out)["error"])

    def test_a_disabled_api_is_an_operator_problem_not_the_users(self):
        wire = Wire([("GET", "/labels", _json(403, {"error": {
            "message": "Gmail API has not been used in project 123 before or it is disabled.",
            "errors": [{"reason": "accessNotConfigured"}],
        }}))])
        out, _ = run(gmail.gmail_list_labels, {}, wire)
        self.assertEqual(parsed(out)["code"], "api_disabled")

    def test_a_5xx_is_retried_once(self):
        wire = Wire([("GET", "/labels", [
            _json(503, {"error": {"message": "backend"}}),
            _json(200, {"labels": [{"id": "INBOX", "name": "INBOX", "type": "system"}]}),
        ])])
        out, _ = run(gmail.gmail_list_labels, {}, wire)
        self.assertEqual(parsed(out)["labels"][0]["id"], "INBOX")

    def test_no_credential_is_credential_missing(self):
        with patch.object(client, "_load_credential", return_value=None), \
             patch.object(client, "_send", new=AsyncMock()) as send:
            out = async_to_sync(gmail.gmail_list_labels)({}, {"user_id": 7})
        self.assertEqual(parsed(out)["code"], "credential_missing")
        send.assert_not_called()

    def test_the_bearer_is_sent_and_never_returned(self):
        wire = Wire([("GET", "/labels", _json(200, {"labels": []}))])
        out, _ = run(gmail.gmail_list_labels, {}, wire, token="ya29.SECRET")
        self.assertEqual(wire.calls[0][2]["headers"]["Authorization"], "Bearer ya29.SECRET")
        self.assertNotIn("SECRET", out)


class GmailReadTests(SimpleTestCase):
    def test_a_multipart_message_reads_as_text_with_attachment_names(self):
        message = {
            "id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
            "payload": {
                "mimeType": "multipart/mixed",
                "headers": [{"name": "From", "value": "Alice <a@x.com>"},
                            {"name": "Subject", "value": "Invoice"}],
                "parts": [
                    {"mimeType": "multipart/alternative", "parts": [
                        {"mimeType": "text/plain", "body": {"data": _b64("Hello, plain.")}},
                        {"mimeType": "text/html", "body": {"data": _b64("<p>Hello, html.</p>")}},
                    ]},
                    {"mimeType": "application/pdf", "filename": "inv.pdf", "body": {"size": 1200}},
                ],
            },
        }
        wire = Wire([("GET", "/threads/t1", _json(200, {"messages": [message]}))])
        out, _ = run(gmail.gmail_get_thread, {"thread_id": "t1"}, wire)
        [m] = parsed(out)["messages"]
        self.assertEqual(m["body"], "Hello, plain.")
        self.assertEqual(m["subject"], "Invoice")
        self.assertEqual(m["attachments"][0]["filename"], "inv.pdf")

    def test_html_only_mail_is_not_an_empty_email(self):
        text = gmail.message_text({"payload": {
            "mimeType": "text/html", "body": {"data": _b64("<h1>Sale</h1><p>50% off</p>")},
        }})
        self.assertIn("Sale", text["body"])
        self.assertIn("50% off", text["body"])
        self.assertNotIn("<p>", text["body"])

    def test_search_summarises_each_thread(self):
        thread = {"messages": [
            {"labelIds": ["UNREAD"], "snippet": "first",
             "payload": {"headers": [{"name": "Subject", "value": "Hi"},
                                     {"name": "From", "value": "b@x.com"}]}},
            {"labelIds": [], "snippet": "latest",
             "payload": {"headers": [{"name": "Date", "value": "Tue"}]}},
        ]}
        wire = Wire([
            ("GET", "/threads/t9", _json(200, thread)),
            ("GET", "/threads", _json(200, {"threads": [{"id": "t9"}]})),
        ])
        out, _ = run(gmail.gmail_search_threads, {"query": "is:unread"}, wire)
        [summary] = parsed(out)["threads"]
        self.assertEqual(summary["subject"], "Hi")
        self.assertEqual(summary["snippet"], "latest")
        self.assertTrue(summary["unread"])
        self.assertEqual(summary["message_count"], 2)
        self.assertEqual(wire.calls[0][2]["params"]["q"], "is:unread")


class GmailWriteTests(SimpleTestCase):
    def _raw(self, wire, index=-1) -> email.message.Message:
        body = wire.calls[index][2]["json"]
        raw = body.get("raw") or body["message"]["raw"]
        return email.message_from_bytes(base64.urlsafe_b64decode(raw + "=="))

    def test_send_builds_a_real_message(self):
        wire = Wire([("POST", "/messages/send", _json(200, {"id": "s1", "threadId": "t1"}))])
        out, _ = run(gmail.gmail_send_message,
                     {"to": "Bob <bob@x.com>", "subject": "Hi", "body": "Body text"}, wire)
        self.assertEqual(parsed(out)["message_id"], "s1")
        msg = self._raw(wire)
        self.assertEqual(msg["To"], "Bob <bob@x.com>")
        self.assertEqual(msg["Subject"], "Hi")
        self.assertIn("Body text", msg.get_payload(decode=True).decode())

    def test_a_reply_stays_in_its_thread(self):
        original = {"threadId": "t7", "payload": {"headers": [
            {"name": "Message-ID", "value": "<abc@mail>"},
            {"name": "Subject", "value": "Plans"},
            {"name": "From", "value": "carol@x.com"},
        ]}}
        wire = Wire([
            ("GET", "/messages/m7", _json(200, original)),
            ("POST", "/drafts", _json(200, {"id": "d1", "message": {"id": "m8"}})),
        ])
        run(gmail.gmail_create_draft, {"reply_to_message_id": "m7", "body": "Sounds good"}, wire)
        body = wire.calls[-1][2]["json"]["message"]
        self.assertEqual(body["threadId"], "t7")
        msg = self._raw(wire)
        self.assertEqual(msg["In-Reply-To"], "<abc@mail>")
        self.assertEqual(msg["Subject"], "Re: Plans")
        self.assertEqual(msg["To"], "carol@x.com")

    def test_a_line_break_cannot_inject_a_header(self):
        wire = Wire([])
        out, _ = run(gmail.gmail_send_message,
                     {"to": "a@x.com", "subject": "Hi\r\nBcc: evil@x.com", "body": "x"}, wire)
        self.assertEqual(parsed(out)["code"], "bad_request")
        self.assertEqual(wire.calls, [])

    def test_labels_resolve_by_name(self):
        wire = Wire([
            ("GET", "/labels", _json(200, {"labels": [{"id": "Label_5", "name": "Receipts"},
                                                      {"id": "UNREAD", "name": "UNREAD"}]})),
            ("POST", "/modify", _json(200, {})),
        ])
        out, _ = run(gmail.gmail_modify_labels,
                     {"message_id": "m1", "add_labels": ["receipts"], "remove_labels": ["UNREAD"]},
                     wire)
        self.assertEqual(parsed(out)["added"], ["Label_5"])
        self.assertEqual(wire.calls[-1][2]["json"],
                         {"addLabelIds": ["Label_5"], "removeLabelIds": ["UNREAD"]})


class DriveTests(SimpleTestCase):
    def test_a_google_doc_is_exported_not_downloaded(self):
        wire = Wire([
            ("GET", "/export", httpx.Response(200, content=b"Doc body text")),
            ("GET", "/files/d1", _json(200, {"id": "d1", "name": "Plan",
                                            "mimeType": "application/vnd.google-apps.document"})),
        ])
        out, _ = run(drive.drive_read_file_content, {"file_id": "d1"}, wire)
        self.assertEqual(parsed(out)["content"], "Doc body text")
        self.assertEqual(wire.calls[-1][2]["params"], {"mimeType": "text/plain"})

    def test_an_uploaded_pdf_goes_through_the_kb_extractor(self):
        wire = Wire([
            ("GET", "/files/p1", [
                _json(200, {"id": "p1", "name": "r.pdf", "mimeType": "application/pdf"}),
                httpx.Response(200, content=b"%PDF-1.4 ..."),
            ]),
        ])
        with patch.object(drive, "_extract", return_value="Extracted PDF text") as extract:
            out, _ = run(drive.drive_read_file_content, {"file_id": "p1"}, wire)
        self.assertEqual(parsed(out)["content"], "Extracted PDF text")
        extract.assert_called_once()

    def test_a_long_file_is_read_in_windows(self):
        text = "x" * 50_000
        wire = Wire([("GET", "/files/t1", [
            _json(200, {"id": "t1", "name": "a.txt", "mimeType": "text/plain"}),
            httpx.Response(200, content=text.encode()),
        ])])
        out, _ = run(drive.drive_read_file_content, {"file_id": "t1"}, wire)
        data = parsed(out)
        self.assertEqual(data["total_chars"], 50_000)
        self.assertEqual(data["next_offset"], len(data["content"]))

    def test_an_oversized_download_is_refused_not_truncated(self):
        wire = Wire([("GET", "/files/big", [
            _json(200, {"id": "big", "name": "big.pdf", "mimeType": "application/pdf"}),
            httpx.Response(200, content=b"0" * (client.DEFAULT_MAX_BYTES + 1)),
        ])])
        out, _ = run(drive.drive_read_file_content, {"file_id": "big"}, wire)
        self.assertEqual(parsed(out)["code"], "too_large")

    def test_search_escapes_quotes_in_the_query(self):
        wire = Wire([("GET", "/files", _json(200, {"files": []}))])
        run(drive.drive_search_files, {"query": "bob's plan"}, wire)
        self.assertIn("bob\\'s plan", wire.calls[0][2]["params"]["q"])

    def test_sheets_without_a_range_reads_the_first_tab(self):
        wire = Wire([
            ("GET", "/values/", _json(200, {"range": "'Q1'!A1:B2", "values": [["a", "b"]]})),
            ("GET", "/spreadsheets/s1", _json(200, {"sheets": [
                {"properties": {"title": "Q1"}}, {"properties": {"title": "Q2"}},
            ]})),
        ])
        out, _ = run(drive.sheets_get_values, {"spreadsheet_id": "s1"}, wire)
        data = parsed(out)
        self.assertEqual(data["tabs"], ["Q1", "Q2"])
        self.assertEqual(data["values"], [["a", "b"]])


class CalendarTests(SimpleTestCase):
    def test_an_all_day_event_uses_date_not_datetime(self):
        wire = Wire([("POST", "/events", _json(200, {"id": "e1", "start": {"date": "2026-09-20"},
                                                     "end": {"date": "2026-09-21"}}))])
        out, _ = run(calendar.calendar_create_event,
                     {"summary": "Offsite", "start": "2026-09-20", "end": "2026-09-21"}, wire)
        body = wire.calls[0][2]["json"]
        self.assertEqual(body["start"], {"date": "2026-09-20"})
        self.assertTrue(parsed(out)["all_day"])
        self.assertEqual(wire.calls[0][2]["params"]["sendUpdates"], "all")

    def test_a_timed_event_carries_its_zone(self):
        wire = Wire([("POST", "/events", _json(200, {"id": "e2"}))])
        run(calendar.calendar_create_event, {
            "summary": "Standup", "start": "2026-09-20T09:00:00", "end": "2026-09-20T09:15:00",
            "time_zone": "Asia/Kolkata",
        }, wire)
        self.assertEqual(wire.calls[0][2]["json"]["start"],
                         {"dateTime": "2026-09-20T09:00:00", "timeZone": "Asia/Kolkata"})

    def test_responding_to_an_event_you_are_not_invited_to_says_so(self):
        wire = Wire([("GET", "/events/e3", _json(200, {"id": "e3", "attendees": [
            {"email": "someone@x.com"},
        ]}))])
        out, _ = run(calendar.calendar_respond_to_event,
                     {"event_id": "e3", "response": "accepted"}, wire)
        self.assertEqual(parsed(out)["code"], "bad_request")
        self.assertEqual(len(wire.calls), 1)

    def test_listing_defaults_to_upcoming_events(self):
        wire = Wire([("GET", "/events", _json(200, {"items": []}))])
        run(calendar.calendar_list_events, {}, wire)
        params = wire.calls[0][2]["params"]
        self.assertEqual(params["singleEvents"], "true")
        self.assertTrue(params["timeMin"])
