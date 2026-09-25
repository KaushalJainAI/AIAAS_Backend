"""
The account-security fixes from the 2026-09-25 whole-project review.

Each class pins one finding in `docs/SECURITY_REVIEW_FIX_PLAN.md`, named by its
id, so a later change that reopens one fails a test that says which.
"""
from datetime import timedelta
from unittest.mock import AsyncMock, patch

from django.conf import settings
from django.contrib.auth.models import User
from django.core.cache import cache
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient
from rest_framework_simplejwt.tokens import RefreshToken

from core.auth import revocation
from core.models import APIKey, UserProfile

PROFILE = '/api/auth/profile/'


def _old_pair(user, seconds_ago=30):
    """A token pair issued `seconds_ago`, i.e. before any revocation made now."""
    refresh = RefreshToken.for_user(user)
    refresh.set_iat(at_time=timezone.now() - timedelta(seconds=seconds_ago))
    access = refresh.access_token
    access.set_iat(at_time=timezone.now() - timedelta(seconds=seconds_ago))
    return str(access), str(refresh)


class _Base(TestCase):
    def setUp(self):
        cache.clear()
        self.client = APIClient()
        self.user = User.objects.create_user(
            'owner', email='owner@example.com', password='Correct-Horse-9')
        UserProfile.objects.get_or_create(user=self.user)


class RevocationTests(_Base):
    """S2: a password reset ends every session that existed before it."""

    def test_a_token_from_before_the_cutoff_is_refused(self):
        access, _ = _old_pair(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(self.client.get(PROFILE).status_code, 200)

        revocation.revoke(self.user)
        self.assertEqual(self.client.get(PROFILE).status_code, 401)

    def test_a_revoked_refresh_token_cannot_mint_access(self):
        _, refresh = _old_pair(self.user)
        revocation.revoke(self.user)
        r = self.client.post('/api/auth/token/refresh/', {'refresh': refresh})
        self.assertEqual(r.status_code, 401)

    def test_the_fresh_pair_issued_with_a_revocation_works(self):
        revocation.revoke(self.user)
        pair = revocation.fresh_pair(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pair['access']}")
        self.assertEqual(self.client.get(PROFILE).status_code, 200)

    def test_the_cutoff_survives_a_cache_flush(self):
        access, _ = _old_pair(self.user)
        revocation.revoke(self.user)
        cache.clear()
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(self.client.get(PROFILE).status_code, 401)

    def test_password_reset_revokes(self):
        from core.models import PasswordOTP

        access, _ = _old_pair(self.user)
        otp = PasswordOTP(user=self.user, purpose=PasswordOTP.PURPOSE_PASSWORD_RESET,
                          expires_at=timezone.now() + timedelta(minutes=10),
                          is_used=True, verification_token='tok')
        otp.set_otp('123456')
        otp.save()
        r = APIClient().post('/api/auth/password-reset-confirm/', {
            'email': 'owner@example.com', 'verification_token': 'tok',
            'new_password': 'New-Pass-Word-77', 'confirm_password': 'New-Pass-Word-77',
        }, format='json')
        self.assertEqual(r.status_code, 200, r.data)
        self.client.credentials(HTTP_AUTHORIZATION=f'Bearer {access}')
        self.assertEqual(self.client.get(PROFILE).status_code, 401)
        self.assertIsNotNone(UserProfile.objects.get(user=self.user).email_verified_at)


class RefreshRotationTests(_Base):
    """S11: a rotated refresh token is really single-use."""

    def test_blacklist_app_is_installed(self):
        self.assertIn('rest_framework_simplejwt.token_blacklist', settings.INSTALLED_APPS)

    def test_a_used_refresh_token_cannot_be_used_again(self):
        refresh = str(RefreshToken.for_user(self.user))
        first = self.client.post('/api/auth/token/refresh/', {'refresh': refresh})
        self.assertEqual(first.status_code, 200)
        second = self.client.post('/api/auth/token/refresh/', {'refresh': refresh})
        self.assertEqual(second.status_code, 401)


class QueryParamTokenTests(_Base):
    """S3: a token in the URL is not a credential."""

    def test_token_query_param_does_not_authenticate(self):
        access = str(RefreshToken.for_user(self.user).access_token)
        self.assertEqual(self.client.get(f'{PROFILE}?token={access}').status_code, 401)


@patch('credentials.oauth.GoogleOAuthProvider.exchange_code',
       new_callable=AsyncMock, return_value={'access_token': 'g'})
class GoogleLinkingTests(_Base):
    """S1: Google sign-in cannot land the owner in an account someone else made."""

    def _login(self, info):
        with patch('credentials.oauth.GoogleOAuthProvider.get_user_info',
                   new_callable=AsyncMock, return_value=info):
            return APIClient().post('/api/auth/google/', {'code': 'c'}, format='json')

    def test_unverified_google_email_is_refused(self, _x):
        r = self._login({'email': 'owner@example.com', 'email_verified': False})
        self.assertEqual(r.status_code, 400)

    def test_linking_evicts_whoever_registered_the_address(self, _x):
        squatter_access, _ = _old_pair(self.user)
        r = self._login({'email': 'OWNER@example.com', 'email_verified': True})
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data['user']['id'], self.user.id)

        self.user.refresh_from_db()
        self.assertFalse(self.user.has_usable_password())
        self.assertIsNotNone(UserProfile.objects.get(user=self.user).email_verified_at)
        c = APIClient()
        c.credentials(HTTP_AUTHORIZATION=f'Bearer {squatter_access}')
        self.assertEqual(c.get(PROFILE).status_code, 401)
        # The owner's own new session works.
        c.credentials(HTTP_AUTHORIZATION=f"Bearer {r.data['access']}")
        self.assertEqual(c.get(PROFILE).status_code, 200)

    def test_a_verified_account_keeps_its_password(self, _x):
        UserProfile.objects.filter(user=self.user).update(email_verified_at=timezone.now())
        self._login({'email': 'owner@example.com', 'email_verified': True})
        self.user.refresh_from_db()
        self.assertTrue(self.user.check_password('Correct-Horse-9'))

    def test_new_users_get_a_free_username_in_one_query(self, _x):
        User.objects.create_user('sam', email='x@example.com')
        User.objects.create_user('sam1', email='y@example.com')
        r = self._login({'email': 'sam@gmail.com', 'email_verified': True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(User.objects.get(email='sam@gmail.com').username, 'sam2')


class APIKeyTests(_Base):
    """S6 + P1: keys are stored hashed, never listed, and touched lazily."""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def test_only_the_hash_is_stored_and_the_key_still_works(self):
        r = self.client.post('/api/auth/api-keys/', {'name': 'ci'}, format='json')
        self.assertEqual(r.status_code, 201)
        raw = r.data['api_key']
        row = APIKey.objects.get(user=self.user)
        self.assertNotEqual(row.key, raw)
        self.assertEqual(row.key, APIKey.hash_key(raw))

        c = APIClient()
        self.assertEqual(c.get(PROFILE, HTTP_X_API_KEY=raw).status_code, 200)
        self.assertEqual(c.get(PROFILE, HTTP_X_API_KEY=row.key).status_code, 401)

    def test_the_list_never_returns_the_key(self):
        self.client.post('/api/auth/api-keys/', {'name': 'ci'}, format='json')
        r = self.client.get('/api/auth/api-keys/')
        rows = r.data['results'] if isinstance(r.data, dict) else r.data
        self.assertNotIn('key', rows[0])
        self.assertIn('key_prefix', rows[0])

    def test_rotate_returns_a_working_key(self):
        self.client.post('/api/auth/api-keys/', {'name': 'ci'}, format='json')
        row = APIKey.objects.get(user=self.user)
        r = self.client.post(f'/api/auth/api-keys/{row.pk}/rotate/')
        raw = r.data['new_key']
        self.assertEqual(APIKey.objects.get(pk=row.pk).key, APIKey.hash_key(raw))

    def test_last_used_is_not_rewritten_on_every_request(self):
        r = self.client.post('/api/auth/api-keys/', {'name': 'ci'}, format='json')
        raw = r.data['api_key']
        c = APIClient()
        c.get(PROFILE, HTTP_X_API_KEY=raw)
        first = APIKey.objects.get(user=self.user).last_used_at
        c.get(PROFILE, HTTP_X_API_KEY=raw)
        self.assertEqual(APIKey.objects.get(user=self.user).last_used_at, first)


class OTPTests(_Base):
    """S7: codes come from a CSPRNG."""

    def test_otp_uses_secrets(self):
        import inspect

        from core import views

        src = inspect.getsource(views._create_password_otp)
        self.assertIn('secrets.randbelow', src)
        self.assertNotIn('random.randint', src)
