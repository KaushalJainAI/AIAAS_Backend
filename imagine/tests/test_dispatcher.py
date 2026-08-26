"""Dispatcher and worker tests: the sync/async image split.

`run_generation` must leave the row in a terminal state on every path — inline
(no Redis), enqueued (async), or inline again when the broker refuses the
enqueue — and the worker must produce exactly what the inline path would.
"""
from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings

from imagine.models import Generation
from imagine.services.dispatcher import run_generation
from imagine.tasks import generate_image_task, poll_video_generation


def _make_generation(**kwargs):
    user = get_user_model().objects.create_user(username="u", password="pw")
    defaults = {
        "type": "image",
        "prompt": "a fox",
        "model": "black-forest-labs/flux.2-pro",
    }
    return Generation.objects.create(user=user, **{**defaults, **kwargs})


class ImageDispatchSplitTests(TestCase):
    def test_image_runs_inline_when_async_is_off(self):
        """Local dev and tests run without Redis; the call stays in the cycle."""
        gen = _make_generation()
        service = Mock()
        with patch(
            "imagine.services.dispatcher.OpenRouterService.for_user",
            return_value=service,
        ), patch("imagine.services.dispatcher.run_image_generation") as run:
            run_generation(gen)
        run.assert_called_once_with(gen, service)

    @override_settings(RUN_WORKFLOWS_ASYNC=True)
    def test_image_is_enqueued_when_async_is_on(self):
        """Async leaves a `pending` row and hands the call to the worker."""
        gen = _make_generation()
        with patch(
            "imagine.services.dispatcher.OpenRouterService.for_user"
        ), patch("imagine.tasks.generate_image_task.delay") as delay:
            run_generation(gen)
        delay.assert_called_once_with(gen.id)
        gen.refresh_from_db()
        self.assertEqual(gen.status, "pending")

    @override_settings(RUN_WORKFLOWS_ASYNC=True)
    def test_image_falls_back_to_inline_when_the_broker_is_down(self):
        """A `pending` row with no worker behind it is a worse failure than a
        slow request — the cycle does the work instead."""
        gen = _make_generation()
        service = Mock()
        with patch(
            "imagine.services.dispatcher.OpenRouterService.for_user",
            return_value=service,
        ), patch(
            "imagine.tasks.generate_image_task.delay",
            side_effect=Exception("broker down"),
        ), patch("imagine.services.dispatcher.run_image_generation") as run:
            run_generation(gen)
        run.assert_called_once_with(gen, service)
        gen.refresh_from_db()
        self.assertEqual(gen.status, "processing")


class GenerateImageTaskTests(TestCase):
    def test_task_completes_and_records_cost(self):
        gen = _make_generation()
        service = Mock()
        service.generate_image.return_value = {
            "url": "data:image/png;base64,QQ==",
            "cost": 0.02,
        }
        with patch("imagine.tasks.OpenRouterService.for_user", return_value=service):
            generate_image_task.run(gen.id)
        gen.refresh_from_db()
        self.assertEqual(gen.status, "completed")
        self.assertEqual(gen.output_url, "data:image/png;base64,QQ==")
        self.assertEqual(gen.metadata["cost_usd"], 0.02)

    def test_task_records_provider_errors(self):
        gen = _make_generation()
        service = Mock()
        service.generate_image.return_value = {"error": "insufficient credits"}
        with patch("imagine.tasks.OpenRouterService.for_user", return_value=service):
            generate_image_task.run(gen.id)
        gen.refresh_from_db()
        self.assertEqual(gen.status, "failed")
        self.assertEqual(gen.error_message, "insufficient credits")

    def test_task_marks_failed_without_a_credential(self):
        from imagine.services.openrouter import MissingOpenRouterCredentialError

        gen = _make_generation()
        with patch(
            "imagine.tasks.OpenRouterService.for_user",
            side_effect=MissingOpenRouterCredentialError("no key"),
        ):
            generate_image_task.run(gen.id)
        gen.refresh_from_db()
        self.assertEqual(gen.status, "failed")
        self.assertIn("no key", gen.error_message)


class VideoPollTests(TestCase):
    def _gen(self, status="pending"):
        return _make_generation(
            type="video", status=status, job_id="job-1", polling_url="http://poll",
        )

    def test_poll_http_error_is_retried_not_abandoned(self):
        """A poll HTTP failure returns status "error" from the service; it used
        to match no branch and strand the row in pending forever."""
        from celery.exceptions import Retry

        gen = self._gen()
        service = Mock()
        service.poll_video_status.return_value = {"status": "error", "error": "boom"}
        with patch(
            "imagine.tasks.OpenRouterService.for_user", return_value=service
        ), patch("imagine.tasks.poll_video_generation.retry", side_effect=Retry()):
            with self.assertRaises(Retry):
                poll_video_generation.run(gen.id)

        gen.refresh_from_db()
        self.assertEqual(gen.status, "pending")

    def test_poll_marks_failed_when_the_retry_budget_is_exhausted(self):
        """20 retries × 30 s is the polling budget; a job still running after
        it must be marked failed, not left pending forever."""
        from celery.exceptions import MaxRetriesExceededError

        gen = self._gen()
        service = Mock()
        service.poll_video_status.return_value = {"status": "in_progress"}
        with patch(
            "imagine.tasks.OpenRouterService.for_user", return_value=service
        ), patch(
            "imagine.tasks.poll_video_generation.retry",
            side_effect=MaxRetriesExceededError(),
        ):
            poll_video_generation.run(gen.id)

        gen.refresh_from_db()
        self.assertEqual(gen.status, "failed")
        self.assertIn("polling limit", gen.error_message)

    def test_poll_marks_failed_when_an_unexpected_error_is_never_resolved(self):
        from celery.exceptions import MaxRetriesExceededError

        gen = self._gen()
        with patch(
            "imagine.tasks.OpenRouterService.for_user",
            side_effect=Exception("credential blowup"),
        ), patch(
            "imagine.tasks.poll_video_generation.retry",
            side_effect=MaxRetriesExceededError(),
        ):
            poll_video_generation.run(gen.id)

        gen.refresh_from_db()
        self.assertEqual(gen.status, "failed")


class CostThrottleTests(TestCase):
    def test_generate_endpoints_carry_the_cost_throttle(self):
        """The global 1000/hour user throttle is not a cost guard; the spending
        endpoints must also carry the scoped one."""
        from imagine.views import (
            ImagineAgentChatView,
            ImagineAgentResumeView,
            ImagineGenerateThrottle,
            ImagineViewSet,
        )

        create = ImagineViewSet()
        create.action = "create"
        self.assertTrue(
            any(isinstance(t, ImagineGenerateThrottle) for t in create.get_throttles())
        )

        listing = ImagineViewSet()
        listing.action = "list"
        self.assertFalse(
            any(isinstance(t, ImagineGenerateThrottle) for t in listing.get_throttles())
        )

        for view in (ImagineAgentChatView(), ImagineAgentResumeView()):
            self.assertTrue(
                any(isinstance(t, ImagineGenerateThrottle) for t in view.get_throttles())
            )