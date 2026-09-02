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
import random
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


def _send_password_otp_email(user, otp_code, purpose):
    label = 'password reset' if purpose == PasswordOTP.PURPOSE_PASSWORD_RESET else 'password change'
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
                recipient_list=[user.email],
                fail_silently=False,
            )
        except Exception as exc:
            logger.error("Failed to send password OTP email to user %s: %s", user.pk, exc)

    threading.Thread(target=send, daemon=True).start()


def _create_password_otp(user, purpose):
    otp_code = f"{random.randint(100000, 999999)}"
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
    )
    otp_record.set_otp(otp_code)
    otp_record.save()
    _send_password_otp_email(user, otp_code, purpose)
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
            
        email = user_info.get('email')
        if not email:
            return Response({'error': 'No email found in Google account'}, status=status.HTTP_400_BAD_REQUEST)
            
        # 3. Find or Create User
        User = get_user_model()
        try:
            user = User.objects.get(email=email)
        except User.DoesNotExist:
            # Create new user
            username = email.split('@')[0]
            # Ensure unique username
            base_username = username
            counter = 1
            while User.objects.filter(username=username).exists():
                username = f"{base_username}{counter}"
                counter += 1
                
            user = User.objects.create_user(
                username=username,
                email=email,
                first_name=user_info.get('given_name', ''),
                last_name=user_info.get('family_name', '')
            )
            # Create profile
            UserProfile.objects.create(user=user)
            
        # 4. Generate JWT
        refresh = RefreshToken.for_user(user)
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
        
        return Response(
            {'detail': 'Password updated successfully'},
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
            'api_key': serializer.instance.key,  # Show full key only on creation
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
        api_key.key = APIKey.generate_key()
        api_key.key_prefix = api_key.key[:8]
        api_key.save()
        
        return Response({
            'new_key': api_key.key,
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
