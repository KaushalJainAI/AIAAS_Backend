from django.test import TestCase
from django.utils import timezone

from core.http.pagination import decode_cursor, encode_cursor, paginate_keyset
from logs.models import ExecutionLog
from agents.models import SubAgent
from django.contrib.auth import get_user_model


class CursorPaginationTests(TestCase):
    def setUp(self):
        self.user = get_user_model().objects.create_user(
            username="cursor-user",
            email="cursor@example.com",
            password="pass",
        )
        self.workflow = SubAgent.objects.create(user=self.user, name="Cursor Flow")

    def test_cursor_round_trip(self):
        now = timezone.now()
        cursor = encode_cursor(now, 42)
        value, pk = decode_cursor(cursor)
        self.assertEqual(value, now)
        self.assertEqual(pk, 42)

    def test_keyset_pagination_has_no_duplicates_on_same_timestamp(self):
        now = timezone.now()
        logs = [
            ExecutionLog.objects.create(user=self.user, subagent=self.workflow),
            ExecutionLog.objects.create(user=self.user, subagent=self.workflow),
            ExecutionLog.objects.create(user=self.user, subagent=self.workflow),
        ]
        ExecutionLog.objects.filter(id__in=[log.id for log in logs]).update(created_at=now)

        qs = ExecutionLog.objects.filter(user=self.user)
        first = paginate_keyset(qs, limit=2)
        second = paginate_keyset(qs, limit=2, cursor=first.next_cursor)

        seen = {item.id for item in first.items + second.items}
        self.assertEqual(seen, {log.id for log in logs})
        self.assertTrue(first.has_more)
        self.assertFalse(second.has_more)
