"""
Tests for turns that outlive the request that started them.

The behaviour under test is the one the old design could not express: a client
going away must not stop the work. Each test therefore drives the registry
directly rather than through HTTP, because what matters is what happens to the
task once nobody is reading — which is exactly what a test client hides.
"""
from __future__ import annotations

import asyncio

from django.contrib.auth import get_user_model
from django.test import TestCase

from chat.turn import runs
from chat.turn.events import Event

User = get_user_model()


async def _settle() -> None:
    """Yield to the loop so a detached task can make progress."""
    for _ in range(10):
        await asyncio.sleep(0)


class RunRegistryTests(TestCase):
    """The registry itself: lifecycle, buffering, fan-out, isolation."""

    def setUp(self) -> None:
        runs.clear()

    def tearDown(self) -> None:
        runs.clear()

    def test_work_continues_after_every_reader_detaches(self) -> None:
        """The point of the whole module: no listeners is not a reason to stop."""

        async def scenario() -> None:
            released = asyncio.Event()
            finished = asyncio.Event()

            async def work(sink) -> None:
                await sink(Event.CONTENT_CHUNK, {"content": "before "})
                await released.wait()
                await sink(Event.CONTENT_CHUNK, {"content": "after"})
                finished.set()

            run = runs.start("s1", user_id=1, work=work)

            # A reader attaches, takes the first frame, then goes away — the
            # browser-reload case.
            stream = runs.subscribe(run)
            self.assertEqual((await stream.__anext__())[1], {"content": "before "})
            await stream.aclose()
            await _settle()

            self.assertEqual(run.status, "running")

            released.set()
            await asyncio.wait_for(finished.wait(), timeout=1)
            await _settle()

            self.assertEqual(run.status, "done")
            self.assertEqual(runs.partial_answer(run), "before after")

        asyncio.run(scenario())

    def test_attach_replays_then_follows_live(self) -> None:
        """A late reader sees the whole turn, not just the rest of it."""

        async def scenario() -> None:
            gate = asyncio.Event()

            async def work(sink) -> None:
                await sink(Event.CONTENT_CHUNK, {"content": "one"})
                await sink(Event.CONTENT_CHUNK, {"content": "two"})
                await gate.wait()
                await sink(Event.DONE, {"ai_response": None})

            run = runs.start("s2", user_id=1, work=work)
            await _settle()  # let the first two frames buffer

            seen: list[tuple[Event, dict]] = []

            async def reader() -> None:
                async for event, payload in runs.subscribe(run):
                    seen.append((event, payload))

            task = asyncio.ensure_future(reader())
            await _settle()

            # Replayed from the buffer, despite attaching after the fact.
            self.assertEqual([p.get("content") for _, p in seen], ["one", "two"])

            gate.set()
            await asyncio.wait_for(task, timeout=1)

            self.assertEqual(seen[-1][0], Event.DONE)
            self.assertEqual(len(seen), 3)  # replayed frames are not duplicated

        asyncio.run(scenario())

    def test_attach_from_index_skips_frames_the_caller_has(self) -> None:
        async def scenario() -> None:
            async def work(sink) -> None:
                for word in ("a", "b", "c"):
                    await sink(Event.CONTENT_CHUNK, {"content": word})

            run = runs.start("s3", user_id=1, work=work)
            await _settle()

            seen = [p["content"] async for _, p in runs.subscribe(run, from_index=2)]
            self.assertEqual(seen, ["c"])

        asyncio.run(scenario())

    def test_second_send_joins_the_running_turn(self) -> None:
        """Two turns on one session would interleave into one transcript."""

        async def scenario() -> None:
            gate = asyncio.Event()
            started = {"n": 0}

            async def work(sink) -> None:
                started["n"] += 1
                await gate.wait()

            first = runs.start("s4", user_id=1, work=work)
            second = runs.start("s4", user_id=1, work=work)

            self.assertIs(first, second)
            await _settle()
            self.assertEqual(started["n"], 1)

            gate.set()
            await _settle()

        asyncio.run(scenario())

    def test_failure_becomes_an_error_frame_not_a_dead_stream(self) -> None:
        async def scenario() -> None:
            async def work(sink) -> None:
                raise RuntimeError("provider exploded")

            run = runs.start("s5", user_id=1, work=work)
            await _settle()

            self.assertEqual(run.status, "error")
            self.assertEqual(run.frames[-1][0], Event.ERROR)

        asyncio.run(scenario())

    def test_active_keys_are_scoped_to_their_owner(self) -> None:
        async def scenario() -> None:
            gate = asyncio.Event()

            async def work(sink) -> None:
                await gate.wait()

            runs.start("mine", user_id=1, work=work)
            runs.start("theirs", user_id=2, work=work)
            await _settle()

            self.assertEqual(runs.active_keys(1), ["mine"])
            self.assertEqual(runs.active_keys(2), ["theirs"])

            gate.set()
            await _settle()

        asyncio.run(scenario())

    def test_stop_cancels_and_refuses_another_users_run(self) -> None:
        async def scenario() -> None:
            cancelled = asyncio.Event()

            async def work(sink) -> None:
                try:
                    await asyncio.Event().wait()  # never completes on its own
                except asyncio.CancelledError:
                    cancelled.set()
                    raise

            run = runs.start("s6", user_id=1, work=work)
            await _settle()

            self.assertIsNone(await runs.stop("s6", user_id=2))
            self.assertEqual(run.status, "running")

            stopped = await runs.stop("s6", user_id=1)
            self.assertIs(stopped, run)
            self.assertTrue(cancelled.is_set())

            # Left running on purpose: the caller still has to persist the
            # partial answer before the run is finished off.
            self.assertEqual(run.status, "running")
            runs.finish(run, "stopped")
            self.assertEqual(run.status, "stopped")

        asyncio.run(scenario())

    def test_partial_answer_respects_a_content_reset(self) -> None:
        """A retracted preamble is not part of what the user was shown."""

        async def scenario() -> None:
            async def work(sink) -> None:
                await sink(Event.CONTENT_CHUNK, {"content": "let me look that up"})
                await sink(Event.CONTENT_RESET, {})
                await sink(Event.CONTENT_CHUNK, {"content": "the answer"})

            run = runs.start("s7", user_id=1, work=work)
            await _settle()

            self.assertEqual(runs.partial_answer(run), "the answer")

        asyncio.run(scenario())


class InterruptedAnswerTests(TestCase):
    """Stopping keeps the text the user already saw."""

    def setUp(self) -> None:
        self.user = User.objects.create_user(
            username="stopper", email="stop@example.com", password="pw"
        )

    def test_partial_text_is_persisted_and_flagged(self) -> None:
        from asgiref.sync import async_to_sync

        from chat.models import ChatSession
        from chat.turn.pipeline import persist_interrupted_answer

        session = ChatSession.objects.create(user=self.user, title="t")
        message = async_to_sync(persist_interrupted_answer)(
            session, self.user, "half an ans"
        )

        self.assertIsNotNone(message)
        self.assertEqual(message.content, "half an ans")
        self.assertTrue(message.metadata["interrupted"])

    def test_nothing_streamed_writes_nothing(self) -> None:
        from asgiref.sync import async_to_sync

        from chat.models import ChatMessage, ChatSession
        from chat.turn.pipeline import persist_interrupted_answer

        session = ChatSession.objects.create(user=self.user, title="t")
        message = async_to_sync(persist_interrupted_answer)(session, self.user, "   ")

        self.assertIsNone(message)
        self.assertEqual(ChatMessage.objects.filter(session=session).count(), 0)


class StreamEndpointWiringTests(TestCase):
    """
    The HTTP surface around a run: routes resolve and answer sensibly.

    Authentication is by bearer token, as in the browser. A session cookie
    would make the async views resolve `request.user` from the database inside
    the event loop and raise `SynchronousOnlyOperation` — a property of
    `force_login` in tests, not of the views, which never see a session.
    """

    def setUp(self) -> None:
        runs.clear()
        self.user = User.objects.create_user(
            username="attacher", email="attach@example.com", password="pw"
        )
        from rest_framework_simplejwt.tokens import AccessToken

        self.auth = {"HTTP_AUTHORIZATION": f"Bearer {AccessToken.for_user(self.user)}"}
        self.missing = f"{'0' * 8}-0000-0000-0000-{'0' * 12}"

    def tearDown(self) -> None:
        runs.clear()

    def _park(self, key: str, user_id: int) -> runs.ChatRun:
        """
        A registered run with no task behind it.

        `runs.start` needs a running loop to schedule the work; these views
        only read the registry, so the task is the one part not worth faking.
        """
        run = runs.ChatRun(key=key, user_id=user_id)
        runs._runs[key] = run
        return run

    def test_active_runs_lists_only_this_users_sessions(self) -> None:
        self._park("mine", self.user.id)
        self._park("theirs", self.user.id + 1)

        response = self.client.get("/api/chat/runs/", **self.auth)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["active"], ["mine"])

    def test_active_runs_needs_authentication(self) -> None:
        self.assertEqual(self.client.get("/api/chat/runs/").status_code, 401)

    def test_attach_with_no_run_closes_without_frames(self) -> None:
        """The answer is already in the database; there is nothing to replay."""
        response = self.client.post(
            f"/api/chat/sessions/{self.missing}/message/attach/",
            data="{}",
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_attach_refuses_another_users_run(self) -> None:
        self._park("someone-elses", self.user.id + 1)

        response = self.client.post(
            "/api/chat/sessions/someone-elses/message/attach/",
            data="{}",
            content_type="application/json",
            **self.auth,
        )

        self.assertEqual(b"".join(response.streaming_content), b"")

    def test_stop_with_nothing_running_is_a_404(self) -> None:
        response = self.client.post(
            f"/api/chat/sessions/{self.missing}/message/stop/", **self.auth
        )

        self.assertEqual(response.status_code, 404)
