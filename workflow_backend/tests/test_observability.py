"""Error reporting stays off without a DSN and never crashes a process."""
from __future__ import annotations

from unittest import mock

from django.test import SimpleTestCase

from workflow_backend import observability


class InitErrorReportingTests(SimpleTestCase):
    def test_off_without_a_dsn(self):
        with mock.patch.dict("os.environ", {"SENTRY_DSN": ""}), \
                mock.patch("sentry_sdk.init") as init:
            self.assertFalse(observability.init_error_reporting("web"))
        init.assert_not_called()

    def test_on_with_a_dsn_and_never_sends_pii(self):
        with mock.patch.dict("os.environ", {"SENTRY_DSN": "https://k@o0.ingest.sentry.io/1"}), \
                mock.patch("sentry_sdk.init") as init, mock.patch("sentry_sdk.set_tag"):
            self.assertTrue(observability.init_error_reporting("web"))
        self.assertIs(init.call_args.kwargs["send_default_pii"], False)
        self.assertEqual(init.call_args.kwargs["traces_sample_rate"], 0.0)

    def test_a_bad_sample_rate_falls_back_to_zero(self):
        with mock.patch.dict("os.environ", {
            "SENTRY_DSN": "https://k@o0.ingest.sentry.io/1", "SENTRY_TRACES_SAMPLE_RATE": "lots",
        }), mock.patch("sentry_sdk.init") as init, mock.patch("sentry_sdk.set_tag"):
            observability.init_error_reporting("worker")
        self.assertEqual(init.call_args.kwargs["traces_sample_rate"], 0.0)
