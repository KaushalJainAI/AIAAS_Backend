"""The session list is the sidebar, not a bulk export of every transcript.

`ChatSessionSerializer` nests `messages`, and the list used it — so rendering a
history of titles fetched every message of every listed conversation, one query
per session. These pin the two halves: the list carries no transcript and costs
a fixed number of queries however many conversations there are, while the
detail route still returns the whole transcript.
"""
from django.contrib.auth import get_user_model
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework.test import APIClient

from chat.models import ChatMessage, ChatSession

User = get_user_model()


class SessionListTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="lister", email="lister@example.com", password="pw",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _session_with_messages(self, n: int) -> ChatSession:
        session = ChatSession.objects.create(user=self.user, title=f"s{n}")
        for i in range(n):
            ChatMessage.objects.create(session=session, role="user", content=f"m{i}")
        return session

    def _list(self):
        response = self.client.get("/api/chat/sessions/")
        self.assertEqual(response.status_code, 200)
        return response.json()["results"]

    def test_the_list_carries_no_transcript(self):
        self._session_with_messages(3)
        (row,) = self._list()
        self.assertNotIn("messages", row)
        self.assertEqual(row["title"], "s3")

    def test_the_list_costs_the_same_however_many_sessions(self):
        self._session_with_messages(2)
        with CaptureQueriesContext(connection) as few:
            self._list()
        for n in range(3, 10):
            self._session_with_messages(n)
        with CaptureQueriesContext(connection) as many:
            self._list()
        self.assertEqual(len(few), len(many))

    def test_the_detail_still_returns_the_transcript(self):
        session = self._session_with_messages(4)
        response = self.client.get(f"/api/chat/sessions/{session.id}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual([m["content"] for m in response.json()["messages"]],
                         ["m0", "m1", "m2", "m3"])

    def test_the_detail_does_not_query_per_message(self):
        small = self._session_with_messages(2)
        large = self._session_with_messages(12)
        with CaptureQueriesContext(connection) as few:
            self.client.get(f"/api/chat/sessions/{small.id}/")
        with CaptureQueriesContext(connection) as many:
            self.client.get(f"/api/chat/sessions/{large.id}/")
        self.assertEqual(len(few), len(many))
