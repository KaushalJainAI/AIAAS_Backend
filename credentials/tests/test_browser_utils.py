"""Tests for the pure decision logic inside the Selenium login helper.

`looks_like_auth_token` and `login_error_in` used to be a nested function and
an inline keyword loop inside `login_and_extract_tokens`, a 173-line Selenium
routine whose only test mocks it out wholesale. Nothing could reach them, so
nothing did. Pulled out to module level, they are ordinary functions and these
are ordinary tests.

`_collect_tokens` is covered with a stub driver rather than a browser: it takes
only the three calls it needs, so a fake object is enough.
"""
from django.test import SimpleTestCase

from credentials.browser_utils import (
    _IGNORED_KEYS,
    _STRONG_SIGNALS,
    _collect_tokens,
    login_error_in,
    looks_like_auth_token,
)


class LooksLikeAuthTokenTests(SimpleTestCase):
    def test_keeps_the_obvious_auth_keys(self):
        for key in ('access_token', 'refresh_token', 'id_token', 'jwt',
                    'authorization', 'bearer', 'sessionid', 'session_id',
                    'X-Auth-Token', 'apiToken'):
            with self.subTest(key=key):
                self.assertTrue(looks_like_auth_token(key))

    def test_rejects_bare_identity_keys(self):
        # A vault full of these is worse than an empty one: it looks connected
        # and is not.
        for key in ('id', 'uuid', 'uid', 'session', 'user', 'lang',
                    'preference', 'theme'):
            with self.subTest(key=key):
                self.assertFalse(looks_like_auth_token(key))

    def test_rejects_telemetry_keys(self):
        for key in ('device_id', 'ga', '_ga_tracking', 'analytics',
                    'optimizely_data', 'pixel', 'aws_region'):
            with self.subTest(key=key):
                self.assertFalse(looks_like_auth_token(key))

    def test_telemetry_prefix_is_kept_when_it_also_says_token_or_auth(self):
        # The weak-substring rule must not veto a genuine token that happens to
        # carry a vendor prefix.
        self.assertTrue(looks_like_auth_token('device_token'))
        self.assertTrue(looks_like_auth_token('aws_auth'))
        self.assertTrue(looks_like_auth_token('track_auth_token'))

    def test_telemetry_veto_beats_a_strong_signal_that_is_not_token_or_auth(self):
        # This is the only shape where the veto changes the answer: a weak
        # prefix, no 'token'/'auth' anywhere, but one of the other strong
        # signals matching. Without these the veto could be deleted and every
        # other test here would still pass.
        for key in ('ga_jwt', 'track_sessionid', 'device_bearer',
                    'analytic_session_id', 'pixel_jwt', 'optimizely_bearer'):
            with self.subTest(key=key):
                self.assertFalse(looks_like_auth_token(key))

    def test_ignored_keys_guard_is_currently_redundant(self):
        # `_IGNORED_KEYS` cannot change any outcome today: no key in it
        # contains a strong signal, so the final `any()` already rejects them.
        # It is kept as insurance -- widening `_STRONG_SIGNALS` to something
        # like 'session' would make it load-bearing overnight. This test is
        # what tells you that happened, because no behavioural test can cover
        # a branch that is unreachable by construction.
        for key in _IGNORED_KEYS:
            with self.subTest(key=key):
                self.assertFalse(
                    any(signal in key for signal in _STRONG_SIGNALS),
                    f"{key!r} now matches a strong signal -- the _IGNORED_KEYS "
                    f"guard has become load-bearing; cover it with a real test.",
                )

    def test_is_case_insensitive(self):
        self.assertTrue(looks_like_auth_token('ACCESS_TOKEN'))
        self.assertFalse(looks_like_auth_token('UUID'))

    def test_unrelated_keys_are_dropped(self):
        for key in ('cart', 'locale', 'sidebar_width', ''):
            with self.subTest(key=key):
                self.assertFalse(looks_like_auth_token(key))


class LoginErrorInTests(SimpleTestCase):
    def test_detects_a_rejection_phrase(self):
        self.assertEqual(
            login_error_in("Sorry, Invalid Password. Try again."),
            "invalid password",
        )

    def test_is_case_insensitive(self):
        self.assertEqual(login_error_in("LOGIN FAILED"), "login failed")

    def test_returns_none_on_a_clean_page(self):
        self.assertIsNone(login_error_in("Welcome back, you are signed in."))

    def test_returns_the_first_phrase_in_declaration_order(self):
        # Two phrases present: the answer is the earlier one in the tuple, so
        # the message a user sees does not depend on page layout.
        text = "login failed: invalid password"
        self.assertEqual(login_error_in(text), "invalid password")

    def test_empty_page_is_not_an_error(self):
        self.assertIsNone(login_error_in(""))


class _StubDriver:
    """The three calls `_collect_tokens` makes, and nothing else."""

    def __init__(self, local=None, session=None, cookies=None):
        self._scripts = {
            "return window.localStorage;": local,
            "return window.sessionStorage;": session,
        }
        self._cookies = cookies or []

    def execute_script(self, script):
        return self._scripts[script]

    def get_cookies(self):
        return self._cookies


class CollectTokensTests(SimpleTestCase):
    def test_prefixes_by_origin(self):
        driver = _StubDriver(
            local={'access_token': 'A'},
            session={'access_token': 'B'},
            cookies=[{'name': 'access_token', 'value': 'C'}],
        )
        # Same key in three stores must not collapse into one entry.
        self.assertEqual(
            _collect_tokens(driver),
            {'ls_access_token': 'A', 'ss_access_token': 'B',
             'cookie_access_token': 'C'},
        )

    def test_filters_out_non_token_keys(self):
        driver = _StubDriver(
            local={'access_token': 'A', 'theme': 'dark', 'device_id': 'x'},
            session={},
            cookies=[{'name': 'ga', 'value': 'y'}],
        )
        self.assertEqual(_collect_tokens(driver), {'ls_access_token': 'A'})

    def test_tolerates_empty_storage(self):
        # execute_script returns None when the page has no storage object.
        self.assertEqual(_collect_tokens(_StubDriver()), {})

    def test_returns_empty_when_nothing_qualifies(self):
        driver = _StubDriver(local={'cart': '1'}, session={'lang': 'en'},
                             cookies=[{'name': 'uuid', 'value': 'z'}])
        self.assertEqual(_collect_tokens(driver), {})
