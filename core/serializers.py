"""
Serializers for User Management and Authentication

Following NGU backend patterns with DRF serializers for API views.
"""
from rest_framework import serializers
from rest_framework.exceptions import AuthenticationFailed
from rest_framework_simplejwt.serializers import TokenObtainPairSerializer
from rest_framework_simplejwt.tokens import RefreshToken
from django.contrib.auth import get_user_model
from django.contrib.auth.password_validation import validate_password
from .models import UserProfile, APIKey, UsageTracking


User = get_user_model()


# ==================== User Serializers ====================

class UserSerializer(serializers.ModelSerializer):
    """Basic user information"""
    
    class Meta:
        model = User
        fields = ['id', 'username', 'email', 'first_name', 'last_name', 'date_joined']
        read_only_fields = ['id', 'date_joined']


class UserProfileSerializer(serializers.ModelSerializer):
    """User profile with tier, limits, and preferences"""
    user = UserSerializer(read_only=True)
    email = serializers.EmailField(source='user.email', read_only=True)
    username = serializers.CharField(source='user.username', read_only=True)
    
    class Meta:
        model = UserProfile
        fields = [
            'user', 'email', 'username', 'display_name', 'avatar', 'bio',
            'instance_name', 'timezone', 'language',
            'tier', 'compile_limit', 'execute_limit',
            'stream_connections', 'credits_remaining', 'credits_used_total',
            'llm_provider', 'llm_model', 'llm_effort', 'llm_credential_id',
            'vision_provider', 'vision_model',
            'default_temperature', 'default_max_tokens',
            'default_autonomy', 'paused_until',
            'theme_preference', 'accent_color',
            'created_at', 'updated_at'
        ]
        # `credits_remaining` is the platform-key allowance `llm/credits.py`
        # meters against. It was writable here, so any user could PATCH their
        # own balance to whatever they liked and spend the platform key without
        # limit. `llm_credential_id` was written by the retired
        # `orchestrator/settings/update/` and no model call ever read it.
        read_only_fields = [
            'tier', 'compile_limit', 'execute_limit', 'stream_connections',
            'credits_remaining', 'credits_used_total', 'llm_credential_id',
            # Stored, never read, and dangerous if it were: a small output cap
            # makes a reasoning model spend it all thinking and answer with an
            # empty string. Kept on the wire so an older client still parses.
            'default_max_tokens',
            'created_at', 'updated_at'
        ]

    def validate_default_autonomy(self, value):
        """ask | auto | plan. `full` is not a default anyone wakes up in."""
        level = (value or '').strip().lower()
        if level not in ('ask', 'auto', 'plan'):
            raise serializers.ValidationError(
                'Autonomy must be ask, auto or plan.'
            )
        return level

    def validate_timezone(self, value):
        """An IANA zone, or the save is refused.

        This value now drives the chat clock, an agent's sense of "now" and a
        new schedule's default zone, so a typo would move all three silently.
        """
        from core.preferences import zone_is_valid

        zone = (value or '').strip() or 'UTC'
        if not zone_is_valid(zone):
            raise serializers.ValidationError(
                f'"{zone}" is not a timezone name, e.g. "Asia/Kolkata".'
            )
        return zone

    def validate_language(self, value):
        """Stored as a language code, whichever spelling the client sent.

        The model's default is `en` while the Settings page sent `English`, so
        the column held two vocabularies and nothing could read it reliably.
        """
        from core.preferences import language_code

        code = language_code(value)
        if code is None:
            raise serializers.ValidationError('Not a supported language.')
        return code

    def validate_llm_effort(self, value):
        """Reject a level that is not on the ladder; blank means model default.

        Validated against `llm.effort.LADDER` rather than against the profile's
        chosen model, for the same reason the chat session's copy is: the model
        can change in the same PATCH, and a level the model does not serve is
        snapped at call time by `llm.access` rather than refused.
        """
        from llm.effort import normalize

        text = (value or '').strip()
        if not text:
            return ''
        level = normalize(text)
        if level is None:
            raise serializers.ValidationError('Not a reasoning effort level.')
        return level

    def update(self, instance, validated_data):
        # Handle nested user updates if provided in request data
        user_data = self.context['request'].data.get('user')
        if user_data:
            user = instance.user
            if 'first_name' in user_data:
                user.first_name = user_data['first_name']
            if 'last_name' in user_data:
                user.last_name = user_data['last_name']
            if 'email' in user_data:
                email = (user_data['email'] or '').strip()
                # Sign-in and password reset both look accounts up by email, so
                # it changes only through `auth/email/change/`, which sends a
                # code to the new address first. Refused rather than ignored:
                # a save that silently drops a field reads as a save that
                # worked. The same address is fine — the form always sends it.
                if email and email.lower() != (user.email or '').lower():
                    raise serializers.ValidationError(
                        {'email': 'Change your email from the Account tab; '
                                  'it needs a code sent to the new address.'}
                    )
            user.save()
            
        return super().update(instance, validated_data)


class UserRegistrationSerializer(serializers.ModelSerializer):
    """User registration with password validation"""
    password = serializers.CharField(
        write_only=True,
        required=True,
        validators=[validate_password],
        style={'input_type': 'password'}
    )
    password2 = serializers.CharField(
        write_only=True,
        required=True,
        style={'input_type': 'password'}
    )
    
    class Meta:
        model = User
        fields = ['username', 'email', 'password', 'password2', 'first_name', 'last_name']
    
    def validate_email(self, value):
        """One account per address.

        Sign-in (`username_field = 'email'`) and password reset both look an
        account up by email, and nothing stopped a second account registering
        an address the first already held — after which neither lookup can say
        which account is meant.
        """
        email = (value or '').strip()
        if email and User.objects.filter(email__iexact=email).exists():
            raise serializers.ValidationError('An account with this email already exists.')
        return email

    def validate(self, attrs):
        if attrs['password'] != attrs['password2']:
            raise serializers.ValidationError({
                'password': "Passwords don't match."
            })
        return attrs
    
    def create(self, validated_data):
        validated_data.pop('password2')
        password = validated_data.pop('password')
        user = User.objects.create(**validated_data)
        user.set_password(password)
        user.save()
        
        # Create user profile automatically
        UserProfile.objects.create(user=user)
        
        return user


# ==================== Auth Serializers ====================

class CustomTokenObtainPairSerializer(TokenObtainPairSerializer):
    """JWT token with additional user claims and user data in response.
    
    Accepts `email` and `password` instead of the default `username`.
    Raises a 401 AuthenticationFailed (not a 400 ValidationError) on bad
    credentials so the frontend always receives a structured `detail` message.
    """

    username_field = 'email'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Replace the auto-generated username field label with 'email'
        self.fields[self.username_field] = serializers.EmailField()

    @classmethod
    def get_token(cls, user):
        token = super().get_token(user)

        # Add custom claims
        token['email'] = user.email
        token['username'] = user.username

        # Add tier if profile exists
        if hasattr(user, 'profile') and user.profile:
            token['tier'] = user.profile.tier
        else:
            token['tier'] = 'free'

        return token

    def validate(self, attrs):
        email = attrs.get('email', '').strip().lower()
        password = attrs.get('password', '')

        User = get_user_model()

        # Resolve the user by email. Use filter().first() (not .get()) so that
        # any legacy duplicate emails can never raise MultipleObjectsReturned
        # (which surfaced as a 500). Resolve deterministically to the earliest
        # account when more than one matches.
        user = User.objects.filter(email__iexact=email).order_by('id').first()
        if user is None:
            raise AuthenticationFailed(
                'Invalid email or password. Please try again.',
                code='authentication_failed',
            )

        # Check password
        if not user.check_password(password):
            raise AuthenticationFailed(
                'Invalid email or password. Please try again.',
                code='authentication_failed',
            )

        # Check account active
        if not user.is_active:
            raise AuthenticationFailed(
                'This account has been disabled. Please contact support.',
                code='account_disabled',
            )

        # Build tokens manually (bypass default username-based lookup)
        refresh = RefreshToken.for_user(user)
        # Embed custom claims
        refresh['email'] = user.email
        refresh['username'] = user.username
        try:
            refresh['tier'] = user.profile.tier
        except Exception:
            refresh['tier'] = 'free'

        # Get or create profile
        profile, _ = UserProfile.objects.get_or_create(user=user)

        return {
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
        }


class GoogleLoginSerializer(serializers.Serializer):
    """Serializer for Google OAuth login parameters."""
    code = serializers.CharField(required=True)
    redirect_uri = serializers.CharField(required=False)

class ChangePasswordSerializer(serializers.Serializer):
    """Password change validation"""
    old_password = serializers.CharField(required=True, write_only=True)
    verification_token = serializers.CharField(required=True, write_only=True)
    new_password = serializers.CharField(
        required=True,
        write_only=True,
        validators=[validate_password]
    )
    confirm_password = serializers.CharField(required=False, write_only=True)
    
    def validate_old_password(self, value):
        user = self.context['request'].user
        if not user.check_password(value):
            raise serializers.ValidationError('Old password is incorrect')
        return value

    def validate(self, attrs):
        confirm_password = attrs.get('confirm_password')
        if confirm_password is not None and attrs['new_password'] != confirm_password:
            raise serializers.ValidationError({'new_password': 'Passwords do not match.'})
        return attrs


class PasswordOTPRequestSerializer(serializers.Serializer):
    """Request an email OTP by email address."""
    email = serializers.EmailField()


class PasswordOTPVerifySerializer(serializers.Serializer):
    """Verify a 6-digit OTP and exchange it for a short verification token."""
    email = serializers.EmailField(required=False)
    otp_code = serializers.CharField(max_length=6, min_length=6)


class PasswordResetConfirmSerializer(serializers.Serializer):
    """Reset a forgotten password after OTP verification."""
    email = serializers.EmailField()
    verification_token = serializers.CharField()
    new_password = serializers.CharField(write_only=True, required=True, validators=[validate_password])
    confirm_password = serializers.CharField(write_only=True, required=True)

    def validate(self, attrs):
        if attrs['new_password'] != attrs['confirm_password']:
            raise serializers.ValidationError({'new_password': 'Passwords do not match.'})
        return attrs


# ==================== API Key Serializers ====================

class APIKeySerializer(serializers.ModelSerializer):
    """API key management. The key itself is never readable here: only its
    hash is stored, and the plaintext is returned once by create/rotate (S6).
    This serializer used to list `key` and return it on every read."""

    class Meta:
        model = APIKey
        fields = [
            'id', 'name', 'key_prefix', 'is_active',
            'expires_at', 'last_used_at', 'created_at'
        ]
        read_only_fields = ['id', 'key_prefix', 'last_used_at', 'created_at']


class APIKeyCreateSerializer(serializers.ModelSerializer):
    """Create API key - returns full key once"""
    
    class Meta:
        model = APIKey
        fields = ['id', 'name', 'key_prefix', 'expires_at', 'created_at']
        read_only_fields = ['id', 'key_prefix', 'created_at']


# ==================== Usage Serializers ====================

class UsageTrackingSerializer(serializers.ModelSerializer):
    """Usage metrics for a day"""
    
    class Meta:
        model = UsageTracking
        fields = [
            'date', 'compile_count', 'execute_count', 'chat_count',
            'tokens_used', 'credits_used', 'estimated_cost'
        ]
        read_only_fields = [
            'date', 'compile_count', 'execute_count', 'chat_count',
            'tokens_used', 'credits_used', 'estimated_cost'
        ]


class UsageInsightSerializer(serializers.Serializer):
    """Aggregated usage insights for the dashboard"""
    total_executions = serializers.IntegerField()
    total_cost = serializers.DecimalField(max_digits=10, decimal_places=4)
    success_rate = serializers.FloatField()
    hours_saved = serializers.FloatField()
    daily_stats = UsageTrackingSerializer(many=True)
    tier = serializers.CharField()
    credits_remaining = serializers.IntegerField()
