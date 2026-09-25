"""
Authentication Views for Workflow Backend

Following NGU backend patterns with rate limiting and JWT.
"""
from rest_framework import generics, status, viewsets
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import AnonRateThrottle
from rest_framework.throttling import UserRateThrottle
from rest_framework_simplejwt.views import TokenObtainPairView

from django.db.models import Sum
from django.utils import timezone as django_timezone
from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.mail import send_mail
from decimal import Decimal
from datetime import timedelta
import logging
import secrets
import threading
import uuid

from .models import UserProfile, APIKey, UsageTracking, PasswordOTP
from .serializers import (
    UserProfileSerializer,
    UserRegistrationSerializer,
    CustomTokenObtainPairSerializer,
    ChangePasswordSerializer,
    APIKeySerializer,
    APIKeyCreateSerializer,
    UsageTrackingSerializer,
    UsageInsightSerializer,
    GoogleLoginSerializer,
    PasswordOTPRequestSerializer,
    PasswordOTPVerifySerializer,
    PasswordResetConfirmSerializer,
)
from logs.models import ExecutionLog
from core.http.throttling import TestClientExemptMixin


# ==================== CUSTOM THROTTLES ====================

# The throttles below are the ones the auth views actually use.
# `core/http/throttling.py` also defines `LoginThrottle` / `RegistrationThrottle`
# on the same scopes, but no view references them -- so a change made there has
# no effect on any request, which is exactly how the first attempt at the
# test-client lane below appeared to do nothing.
#
# `TestClientExemptMixin` gives an automated E2E client its own lane: the rates
# stay exactly as they are for every ordinary client, and a client presenting
# the configured `X-E2E-Bypass-Token` skips the limit -- and only the limit;
# authentication, permissions and ownership are untouched. The whole mechanism
# is off unless `E2E_THROTTLE_BYPASS_TOKEN` is set, which it is not by default.

class LoginRateThrottle(TestClientExemptMixin, AnonRateThrottle):
    """Throttle for login attempts - prevents brute force attacks"""
    scope = 'login'


class RegisterRateThrottle(TestClientExemptMixin, AnonRateThrottle):
    """Throttle for registration - prevents mass account creation"""
    scope = 'register'


class PasswordResetRateThrottle(AnonRateThrottle):
    """Throttle for anonymous password reset OTP requests and verification."""
    scope = 'password_reset'


class PasswordChangeOTPThrottle(UserRateThrottle):
    """Throttle for authenticated password change OTP verification."""
    scope = 'password_change'


logger = logging.getLogger(__name__)
User = get_user_model()


def _send_password_otp_email(user, otp_code, purpose, recipient=''):
    label = {
        PasswordOTP.PURPOSE_PASSWORD_RESET: 'password reset',
        PasswordOTP.PURPOSE_EMAIL_CHANGE: 'email change',
    }.get(purpose, 'password change')
    subject = f'AIAAS {label.title()} OTP'
    message = (
        f'Your AIAAS OTP for {label} is: {otp_code}\n\n'
        'This code will expire in 10 minutes. If you did not request this, you can ignore this email.'
    )
    from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', '') or getattr(settings, 'EMAIL_HOST_USER', '')

    def send():
        try:
            send_mail(
                subject=subject,
                message=message,
                from_email=from_email,
                recipient_list=[recipient or user.email],
                fail_silently=False,
            )
        except Exception as exc:
            logger.error("Failed to send password OTP email to user %s: %s", user.pk, exc)

    threading.Thread(target=send, daemon=True).start()


def _free_username(User, base: str) -> str:
    """`base`, or `base<n>` for the smallest free n -- in one query (P3).

    The old loop issued an `exists()` per collision, so the tenth "john"
    cost ten round trips to sign in.
    """
    base = base or 'user'
    taken = set(User.objects.filter(username__startswith=base)
                .values_list('username', flat=True))
    if base not in taken:
        return base
    n = 1
    while f'{base}{n}' in taken:
        n += 1
    return f'{base}{n}'


def _claim_verified_email(user, profile) -> None:
    """Record that this account's owner just proved they hold its inbox.

    Signup never verifies an address, so a password account whose email was
    never proven may have been registered by someone who is not its owner --
    who would then be sitting inside the owner's account the moment the owner
    signs in with Google (S1, pre-account takeover). Proving the inbox
    therefore evicts whoever set that password: it is made unusable and every
    earlier token revoked. The real owner can set a new one through the
    forgot-password flow, which reaches the same inbox.
    """
    from core.auth.revocation import revoke

    if profile.email_verified_at is None and user.has_usable_password():
        user.set_unusable_password()
        user.save(update_fields=['password'])
        revoke(user)
    if profile.email_verified_at is None:
        profile.email_verified_at = django_timezone.now()
        profile.save(update_fields=['email_verified_at'])


def _mark_email_verified(user) -> None:
    UserProfile.objects.update_or_create(
        user=user, defaults={'email_verified_at': django_timezone.now()})


def _create_password_otp(user, purpose, target_email=''):
    # `secrets`, not `random`: Mersenne Twister output is predictable from
    # enough earlier outputs, and these codes are what reset a password (S7).
    otp_code = f"{100000 + secrets.randbelow(900000)}"
    PasswordOTP.objects.filter(user=user, purpose=purpose, is_used=False).update(is_used=True)
    PasswordOTP.objects.filter(
        user=user,
        purpose=purpose,
        expires_at__lt=django_timezone.now() - timedelta(hours=24),
    ).delete()
    otp_record = PasswordOTP(
        user=user,
        purpose=purpose,
        expires_at=django_timezone.now() + timedelta(minutes=10),
        target_email=target_email,
    )
    otp_record.set_otp(otp_code)
    otp_record.save()
    _send_password_otp_email(user, otp_code, purpose, recipient=target_email)
    return otp_record


def _verify_password_otp(user, purpose, otp_code):
    otp_record = PasswordOTP.objects.filter(
        user=user,
        purpose=purpose,
        is_used=False,
    ).latest('created_at')

    if otp_record.is_expired:
        return None, Response({'detail': 'OTP has expired. Please request a new one.'}, status=status.HTTP_400_BAD_REQUEST)
    if otp_record.is_locked:
        return None, Response({'detail': 'Too many failed attempts. Please request a new OTP.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)
    if not otp_record.check_otp(otp_code):
        otp_record.failed_attempts += 1
        otp_record.save(update_fields=['failed_attempts'])
        remaining = PasswordOTP.MAX_FAILED_ATTEMPTS - otp_record.failed_attempts
        if remaining > 0:
            return None, Response({'detail': f'Invalid OTP. {remaining} attempt(s) remaining.'}, status=status.HTTP_400_BAD_REQUEST)
        return None, Response({'detail': 'Too many failed attempts. Please request a new OTP.'}, status=status.HTTP_429_TOO_MANY_REQUESTS)

    otp_record.is_used = True
    otp_record.verification_token = str(uuid.uuid4())
    otp_record.save(update_fields=['is_used', 'verification_token'])
    return otp_record, None


# ==================== Auth Views ====================

class UserRegistrationView(generics.CreateAPIView):
    """
    Register a new user.
    
    Rate limited: 3 attempts per minute
    Creates user and associated UserProfile automatically.
    Returns JWT tokens for immediate auth.
    """
    serializer_class = UserRegistrationSerializer
    permission_classes = [AllowAny]
    throttle_classes = [RegisterRateThrottle]
    
    def create(self, request, *args, **kwargs):
        from rest_framework_simplejwt.tokens import RefreshToken
        
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        user = serializer.save()
        
        # Generate tokens for immediate login
        refresh = RefreshToken.for_user(user)
        
        # Get profile
        try:
            profile = user.profile
        except UserProfile.DoesNotExist:
            profile = UserProfile.objects.create(user=user)
        
        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': user.id,
                'email': user.email,
                'name': f"{user.first_name} {user.last_name}".strip() or user.username,
                'tier': profile.tier,
                'credits': profile.credits_remaining,
                'createdAt': user.date_joined.isoformat(),
            },
            'message': 'User registered successfully.'
        }, status=status.HTTP_201_CREATED)


class GoogleLoginView(APIView):
    """
    Exchange Google OAuth2 code for JWT tokens.
    Creates user if they don't exist.
    """
    permission_classes = [AllowAny]
    throttle_classes = [LoginRateThrottle]

    def post(self, request):
        from credentials.oauth import GoogleOAuthProvider
        from django.conf import settings
        from django.contrib.auth import get_user_model
        from rest_framework_simplejwt.tokens import RefreshToken
        from asgiref.sync import async_to_sync
        
        serializer = GoogleLoginSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        
        code = serializer.validated_data['code']
        redirect_uri = serializer.validated_data.get('redirect_uri', settings.GOOGLE_OAUTH_REDIRECT_URI)
        
        provider = GoogleOAuthProvider(redirect_uri=redirect_uri)
        
        try:
            # The provider's methods are async (aiohttp); this sync view must
            # bridge them, or token_data is a coroutine and `'error' in
            # token_data` raises TypeError at runtime.
            token_data = async_to_sync(provider.exchange_code)(code)
        except Exception as e:
             return Response({'error': f'Token exchange failed: {str(e)}'}, status=status.HTTP_400_BAD_REQUEST)
             
        if 'error' in token_data:
             return Response({'error': token_data.get('error_description', 'Unknown OAuth error')}, status=status.HTTP_400_BAD_REQUEST)
             
        access_token = token_data.get('access_token')
        
        # 2. Get User Info
        try:
            user_info = async_to_sync(provider.get_user_info)(access_token)
        except Exception:
            return Response({'error': 'Failed to fetch user info'}, status=status.HTTP_400_BAD_REQUEST)
            
        email = (user_info.get('email') or '').strip()
        if not email:
            return Response({'error': 'No email found in Google account'}, status=status.HTTP_400_BAD_REQUEST)
        # Linking by email is only sound when Google vouches for the address;
        # an unverified one is just a string someone typed (S1).
        if user_info.get('email_verified') is not True:
            return Response({'error': 'Your Google account email is not verified.'},
                            status=status.HTTP_400_BAD_REQUEST)

        # 3. Find or Create User -- case-insensitively, as signup checks.
        User = get_user_model()
        user = User.objects.filter(email__iexact=email).order_by('pk').first()
        if user is None:
            user = User.objects.create_user(
                username=_free_username(User, email.split('@')[0]),
                email=email,
                first_name=user_info.get('given_name', ''),
                last_name=user_info.get('family_name', '')
            )
        profile, _ = UserProfile.objects.get_or_create(user=user)
        _claim_verified_email(user, profile)

        # 4. Generate JWT
        refresh = RefreshToken.for_user(user)

        return Response({
            'access': str(refresh.access_token),
            'refresh': str(refresh),
            'user': {
                'id': user.id,
                'email': user.email,
                'name': f"{user.first_name} {user.last_name}".strip() or user.username,
                'tier': profile.tier,
                'credits': profile.credits_remaining,
                'createdAt': user.date_joined.isoformat(),
            }
        })


class CustomTokenObtainPairView(TokenObtainPairView):
    """
    Custom JWT token view with additional user data.
    
    Rate limited: 5 attempts per minute to prevent brute force.
    Returns access token, refresh token, and user tier.
    """
    serializer_class = CustomTokenObtainPairSerializer
    throttle_classes = [LoginRateThrottle]


class UserProfileView(generics.RetrieveUpdateAPIView):
    """
    Get and update current user's profile.
    
    GET: Returns user profile with tier, limits, and credits
    PATCH: Update basic profile info
    """
    serializer_class = UserProfileSerializer
    permission_classes = [IsAuthenticated]
    
    def get_object(self):
        profile, _ = UserProfile.objects.get_or_create(user=self.request.user)
        return profile


class ChangePasswordView(APIView):
    """Change current user's password after email OTP verification."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeOTPThrottle]
    
    def post(self, request):
        serializer = ChangePasswordSerializer(
            data=request.data,
            context={'request': request}
        )
        serializer.is_valid(raise_exception=True)
        
        user = request.user
        try:
            otp_record = PasswordOTP.objects.get(
                user=user,
                purpose=PasswordOTP.PURPOSE_PASSWORD_CHANGE,
                verification_token=serializer.validated_data['verification_token'],
                is_used=True,
            )
        except PasswordOTP.DoesNotExist:
            return Response({'detail': 'Invalid or expired verification token.'}, status=status.HTTP_400_BAD_REQUEST)

        if otp_record.is_expired:
            return Response({'detail': 'Password change session has expired. Please request a new OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data['new_password'])
        user.save()
        otp_record.verification_token = None
        otp_record.save(update_fields=['verification_token'])
        # The OTP went to the account's inbox, so the email is now proven; and
        # every other session ends (S2). This tab gets a fresh pair.
        _mark_email_verified(user)
        from core.auth.revocation import fresh_pair, revoke

        revoke(user)
        return Response(
            {'detail': 'Password updated successfully', **fresh_pair(user)},
            status=status.HTTP_200_OK
        )


class PasswordChangeOTPRequestView(APIView):
    """Send an OTP to the authenticated user's email before password change."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeOTPThrottle]

    def post(self, request):
        old_password = request.data.get('old_password')
        if not old_password:
            return Response({'detail': 'Current password is required.'}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.check_password(old_password):
            return Response({'detail': 'Old password is incorrect'}, status=status.HTTP_400_BAD_REQUEST)
        if not request.user.email:
            return Response({'detail': 'Your account does not have an email address.'}, status=status.HTTP_400_BAD_REQUEST)

        _create_password_otp(request.user, PasswordOTP.PURPOSE_PASSWORD_CHANGE)
        return Response({'detail': 'OTP sent to your email.'}, status=status.HTTP_200_OK)


class PasswordChangeOTPVerifyView(APIView):
    """Verify OTP for authenticated password change."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeOTPThrottle]

    def post(self, request):
        serializer = PasswordOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            otp_record, error_response = _verify_password_otp(
                request.user,
                PasswordOTP.PURPOSE_PASSWORD_CHANGE,
                serializer.validated_data['otp_code'],
            )
        except PasswordOTP.DoesNotExist:
            return Response({'detail': 'Invalid OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        if error_response:
            return error_response
        return Response({
            'detail': 'OTP verified successfully.',
            'verification_token': otp_record.verification_token,
        }, status=status.HTTP_200_OK)


def _email_taken(email, user) -> bool:
    return User.objects.filter(email__iexact=email).exclude(pk=user.pk).exists()


class EmailChangeRequestView(APIView):
    """Send a code to a new address before it can become the account's email.

    The profile PATCH used to set `user.email` straight from the request, and
    that address is what sign-in and password reset look accounts up by — so
    a typo locked someone out, and a session left open on a shared machine was
    enough to point the account's reset at someone else's inbox. The code goes
    to the *new* address (proving it is theirs), and the current password is
    asked for first where the account has one (a Google-only account does not).
    """
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeOTPThrottle]

    def post(self, request):
        from django.core.exceptions import ValidationError as DjangoValidationError
        from django.core.validators import validate_email

        email = (request.data.get('new_email') or '').strip()
        try:
            validate_email(email)
        except DjangoValidationError:
            return Response({'detail': 'Enter a valid email address.'},
                            status=status.HTTP_400_BAD_REQUEST)
        user = request.user
        if email.lower() == (user.email or '').lower():
            return Response({'detail': 'That is already your email address.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if user.has_usable_password():
            password = request.data.get('password') or ''
            if not user.check_password(password):
                return Response({'detail': 'Your current password is incorrect.'},
                                status=status.HTTP_400_BAD_REQUEST)
        if _email_taken(email, user):
            return Response({'detail': 'That email address is already in use.'},
                            status=status.HTTP_400_BAD_REQUEST)

        _create_password_otp(user, PasswordOTP.PURPOSE_EMAIL_CHANGE, target_email=email)
        return Response({'detail': f'A code was sent to {email}.'},
                        status=status.HTTP_200_OK)


class EmailChangeConfirmView(APIView):
    """Apply the new address once the code sent to it comes back."""
    permission_classes = [IsAuthenticated]
    throttle_classes = [PasswordChangeOTPThrottle]

    def post(self, request):
        serializer = PasswordOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        try:
            otp_record, error_response = _verify_password_otp(
                request.user, PasswordOTP.PURPOSE_EMAIL_CHANGE,
                serializer.validated_data['otp_code'],
            )
        except PasswordOTP.DoesNotExist:
            return Response({'detail': 'Request a code first.'},
                            status=status.HTTP_400_BAD_REQUEST)
        if error_response:
            return error_response

        email = otp_record.target_email
        # Checked again: the address may have been taken in the ten minutes
        # the code was valid for.
        if not email or _email_taken(email, request.user):
            return Response({'detail': 'That email address is no longer available.'},
                            status=status.HTTP_400_BAD_REQUEST)
        request.user.email = email
        request.user.save(update_fields=['email'])
        # The code reached the new address; and the address is what sign-in and
        # reset key on, so every other session ends with the old one (S2).
        _mark_email_verified(request.user)
        from core.auth.revocation import fresh_pair, revoke

        revoke(request.user)
        return Response({'detail': 'Email address updated.', 'email': email,
                         **fresh_pair(request.user)},
                        status=status.HTTP_200_OK)


class PasswordResetRequestView(APIView):
    """Send an OTP to the user's email for forgot-password reset."""
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

    def post(self, request):
        serializer = PasswordOTPRequestSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data['email']

        try:
            user = User.objects.get(email=email)
            _create_password_otp(user, PasswordOTP.PURPOSE_PASSWORD_RESET)
        except User.DoesNotExist:
            User().set_password('dummy_password')

        return Response(
            {'detail': 'If an account exists with this email, an OTP has been sent.'},
            status=status.HTTP_200_OK
        )


class PasswordResetVerifyView(APIView):
    """Verify forgot-password OTP and return a short verification token."""
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

    def post(self, request):
        serializer = PasswordOTPVerifySerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        email = serializer.validated_data.get('email')
        if not email:
            return Response({'email': ['This field is required.']}, status=status.HTTP_400_BAD_REQUEST)

        try:
            user = User.objects.get(email=email)
            otp_record, error_response = _verify_password_otp(
                user,
                PasswordOTP.PURPOSE_PASSWORD_RESET,
                serializer.validated_data['otp_code'],
            )
        except (User.DoesNotExist, PasswordOTP.DoesNotExist):
            return Response({'detail': 'Invalid OTP or email.'}, status=status.HTTP_400_BAD_REQUEST)

        if error_response:
            return error_response
        return Response({
            'detail': 'OTP verified successfully. You may proceed to reset password.',
            'verification_token': otp_record.verification_token,
        }, status=status.HTTP_200_OK)


class PasswordResetConfirmView(APIView):
    """Reset forgotten password after OTP verification."""
    permission_classes = [AllowAny]
    throttle_classes = [PasswordResetRateThrottle]

    def post(self, request):
        serializer = PasswordResetConfirmSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        email = serializer.validated_data['email']
        verification_token = serializer.validated_data['verification_token']

        try:
            user = User.objects.get(email=email)
            otp_record = PasswordOTP.objects.get(
                user=user,
                purpose=PasswordOTP.PURPOSE_PASSWORD_RESET,
                verification_token=verification_token,
                is_used=True,
            )
        except (User.DoesNotExist, PasswordOTP.DoesNotExist):
            return Response({'detail': 'Invalid OTP or email.'}, status=status.HTTP_400_BAD_REQUEST)

        if otp_record.is_expired:
            return Response({'detail': 'Password reset session has expired. Please request a new OTP.'}, status=status.HTTP_400_BAD_REQUEST)

        user.set_password(serializer.validated_data['new_password'])
        user.save()
        otp_record.verification_token = None
        otp_record.save(update_fields=['verification_token'])
        # A reset is how someone locks out whoever has their account, so it
        # must end every session that exists (S2) -- and the OTP proved the inbox.
        _mark_email_verified(user)
        from core.auth.revocation import revoke

        revoke(user)
        return Response({'detail': 'Password has been reset successfully. You can now login.'}, status=status.HTTP_200_OK)


# ==================== API Key Views ====================

class APIKeyViewSet(viewsets.ModelViewSet):
    """
    Manage user's API keys.
    
    LIST: Get all user's API keys (key value hidden)
    CREATE: Generate a new API key (key shown once)
    DELETE: Revoke an API key
    """
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return APIKey.objects.filter(user=self.request.user)
    
    def get_serializer_class(self):
        if self.action == 'create':
            return APIKeyCreateSerializer
        return APIKeySerializer
    
    def perform_create(self, serializer):
        serializer.save(user=self.request.user)
    
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        self.perform_create(serializer)
        
        return Response({
            'api_key': serializer.instance.plaintext,  # only the hash is stored (S6)
            'message': 'API key created. Save this key - it will not be shown again.',
            'data': APIKeySerializer(serializer.instance).data
        }, status=status.HTTP_201_CREATED)


class APIKeyRotateView(APIView):
    """Rotate (regenerate) an existing API key"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request, pk):
        try:
            api_key = APIKey.objects.get(pk=pk, user=request.user)
        except APIKey.DoesNotExist:
            return Response(
                {'detail': 'API key not found'},
                status=status.HTTP_404_NOT_FOUND
            )
        
        # Generate new key
        old_prefix = api_key.key_prefix
        new_key = api_key.set_new_key()
        api_key.save()
        
        return Response({
            'new_key': new_key,
            'old_prefix': old_prefix,
            'message': 'API key rotated. Save this key - it will not be shown again.'
        }, status=status.HTTP_200_OK)


class AvatarUploadView(APIView):
    """Upload or update user profile avatar"""
    permission_classes = [IsAuthenticated]
    
    def post(self, request):
        if 'avatar' not in request.FILES:
            return Response({'error': 'No avatar file provided'}, status=status.HTTP_400_BAD_REQUEST)
        
        profile, _ = UserProfile.objects.get_or_create(user=request.user)
        profile.avatar = request.FILES['avatar']
        profile.save()
        
        return Response({
            'avatar_url': request.build_absolute_uri(profile.avatar.url) if profile.avatar else None,
            'message': 'Avatar uploaded successfully'
        })


class UsageInsightsView(APIView):
    """
    Get aggregated usage insights for the current user.
    Calculates total executions, costs, success rates, and ROI.
    """
    permission_classes = [IsAuthenticated]
    
    def get(self, request):
        user = request.user
        profile, _ = UserProfile.objects.get_or_create(user=user)
        
        # Aggregate daily stats from UsageTracking
        usage_qs = UsageTracking.objects.filter(user=user).order_by('-date')
        daily_stats = usage_qs[:30]  # Last 30 days
        
        totals = usage_qs.aggregate(
            total_exec=Sum('execute_count'),
            total_cost=Sum('estimated_cost'),
            total_compile=Sum('compile_count'),
            total_chat=Sum('chat_count')
        )
        
        total_executions = totals['total_exec'] or 0
        total_cost = totals['total_cost'] or Decimal('0.0000')
        
        # Calculate Success Rate from ExecutionLog
        exec_logs = ExecutionLog.objects.filter(user=user)
        total_finished = exec_logs.filter(
            status__in=['completed', 'failed', 'timeout', 'cancelled']
        ).count()
        
        if total_finished > 0:
            completed = exec_logs.filter(status='completed').count()
            success_rate = (completed / total_finished) * 100
        else:
            success_rate = 100.0  # Default if no executions yet
            
        # ROI: Estimate hours saved (avg 2 mins per execution)
        hours_saved = (total_executions * 2.0) / 60.0
        
        data = {
            'total_executions': total_executions,
            'total_cost': total_cost,
            'success_rate': success_rate,
            'hours_saved': hours_saved,
            'daily_stats': daily_stats,
            'tier': profile.tier,
            'credits_remaining': profile.credits_remaining
        }
        
        serializer = UsageInsightSerializer(data)
        return Response(serializer.data)


# ==================== Usage Views ====================

class UsageTrackingView(generics.ListAPIView):
    """
    Get usage metrics for current user.
    
    Returns daily usage records sorted by date descending.
    """
    serializer_class = UsageTrackingSerializer
    permission_classes = [IsAuthenticated]
    
    def get_queryset(self):
        return UsageTracking.objects.filter(user=self.request.user)[:30]  # Last 30 days


# ─────────────────────────────────────────────────────────────────────────────
# What the assistant remembers about you
# ─────────────────────────────────────────────────────────────────────────────

class UserMemoryView(APIView):
    """List and delete the durable facts stored about the requesting user.

    A memory store the person cannot see is one they cannot correct, and every
    fact here is injected into the system prompt of every future turn — so a
    wrong one keeps being wrong, in every conversation, until someone removes
    it. The model can call `forget_about_user`, but that only helps if the user
    knows what is there to forget.

    Read and delete only. There is deliberately no create: a fact typed into a
    settings screen is a preference, and preferences belong on the profile
    where they are validated. This surface exists to *audit and correct* what
    the assistant inferred, which is a different job.
    """

    permission_classes = [IsAuthenticated]

    def get(self, request):
        from .models import UserMemory

        rows = UserMemory.objects.filter(user=request.user)
        return Response({
            'memories': [
                {
                    'id': row.id,
                    'text': row.text,
                    'category': row.category,
                    'source': row.source,
                    'updated_at': row.updated_at,
                }
                for row in rows
            ],
            # So a UI can say "24 of 25" rather than leaving the user to guess
            # why an old fact vanished.
            'max_per_category': UserMemory.MAX_PER_CATEGORY,
        })

    def delete(self, request, memory_id=None):
        """Forget one fact, or all of them.

        Scoped by `user=request.user` in the query rather than checked after
        the fetch, so somebody else's id and a nonexistent id are the same
        404 — a distinguishable "exists but not yours" is an oracle.
        """
        from .models import UserMemory

        rows = UserMemory.objects.filter(user=request.user)
        if memory_id is not None:
            deleted, _ = rows.filter(id=memory_id).delete()
            if not deleted:
                return Response({'detail': 'No such memory.'},
                                status=status.HTTP_404_NOT_FOUND)
            return Response(status=status.HTTP_204_NO_CONTENT)

        rows.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
