from django.db import models
from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.utils import timezone
from django.core.validators import MinValueValidator, MaxValueValidator
import secrets


class UserProfile(models.Model):
    """
    Extended user profile with API keys, tier information, and usage limits.
    Links to Django's built-in User model via OneToOne relationship.
    """
    TIER_CHOICES = [
        ('free', 'Free'),
        ('pro', 'Pro'),
        ('enterprise', 'Enterprise'),
    ]
    
    user = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='profile'
    )
    tier = models.CharField(
        max_length=20,
        choices=TIER_CHOICES,
        default='free',
        help_text='User subscription tier'
    )
    
    # Rate Limits (per minute)
    compile_limit = models.IntegerField(
        default=10,
        validators=[MinValueValidator(0)],
        help_text='Workflow compilations per minute'
    )
    execute_limit = models.IntegerField(
        default=5,
        validators=[MinValueValidator(0)],
        help_text='Workflow executions per minute'
    )
    stream_connections = models.IntegerField(
        default=5,
        validators=[MinValueValidator(0)],
        help_text='Maximum concurrent streaming connections'
    )
    
    # Credits/Usage
    credits_remaining = models.IntegerField(
        default=100,
        validators=[MinValueValidator(0)],
        help_text='API credits remaining'
    )
    credits_used_total = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Total credits used historically'
    )
    
    # Credential/AI Preferences (Global for Orchestrator/King)
    # Repointed from NVIDIA to OpenRouter's free router on 2026-09-03, for the
    # reason `ChatSession` documents: every `nvidia/*` row is 410 upstream, so
    # the old default named a model that could not answer.
    llm_provider = models.CharField(
        max_length=30,
        default='openrouter',
        help_text='Global LLM provider for internal AI reasoning'
    )
    llm_model = models.CharField(
        max_length=150,
        default='openrouter/free',
        help_text='Global LLM model for internal AI reasoning'
    )
    #: How hard that model is asked to think, from `llm.effort.LADDER`. Blank
    #: means the model's own default; `medium` is the shipped standard, the
    #: same rung a new chat session starts on. A model with no effort control
    #: ignores it, so this is safe to carry across a model change.
    llm_effort = models.CharField(
        max_length=10,
        blank=True,
        default='medium',
        help_text='Default reasoning effort (blank = let the model decide)'
    )
    llm_credential_id = models.IntegerField(
        null=True,
        blank=True,
        help_text='Default credential ID for King Orchestrator'
    )

    # Vision witness — the model a text-only main agent interrogates about an
    # image it cannot see. Separate from `llm_model` on purpose: the witness is
    # picked for price and latency, the main model for reasoning, and forcing
    # one field to serve both would make every text-only chat lose its eyes.
    vision_provider = models.CharField(
        max_length=50,
        blank=True,
        default='nvidia',
        help_text='Provider for the vision witness model'
    )
    vision_model = models.CharField(
        max_length=120,
        blank=True,
        default='nvidia/nemotron-nano-12b-v2-vl',
        help_text='Vision model asked about images the main model cannot see'
    )

    # Identity
    display_name = models.CharField(
        max_length=100,
        blank=True,
        help_text='Public display name'
    )
    avatar = models.ImageField(
        upload_to='avatars/',
        null=True,
        blank=True,
        help_text='Profile picture'
    )
    bio = models.TextField(
        blank=True,
        help_text='User biography'
    )

    # Environment / Localization
    instance_name = models.CharField(
        max_length=100,
        default='AIAAS Instance',
        help_text='Name of the platform instance for this user'
    )
    timezone = models.CharField(
        max_length=50,
        default='UTC',
        help_text='User preferred timezone'
    )
    language = models.CharField(
        max_length=10,
        default='en',
        help_text='User preferred language code (e.g. en, es)'
    )

    # AI Governance Defaults
    default_temperature = models.FloatField(
        default=0.7,
        validators=[MinValueValidator(0.0), MaxValueValidator(2.0)],
        help_text='Default temperature for AI reasoning'
    )
    default_max_tokens = models.IntegerField(
        default=2048,
        validators=[MinValueValidator(1)],
        help_text='Default max tokens for AI responses'
    )

    # Appearance
    THEME_CHOICES = [
        ('light', 'Light'),
        ('dark', 'Dark'),
        ('system', 'System'),
    ]
    theme_preference = models.CharField(
        max_length=10,
        choices=THEME_CHOICES,
        default='system'
    )
    accent_color = models.CharField(
        max_length=20,
        default='blue',
        help_text='Preferred accent color (e.g. blue, magenta)'
    )

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'User Profile'
        verbose_name_plural = 'User Profiles'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['tier']),
            models.Index(fields=['user', 'tier']),
        ]

    def __str__(self):
        return f"{self.user.username} ({self.tier})"

    @property
    def is_enterprise(self):
        """Check if user has enterprise tier"""
        return self.tier == 'enterprise'

    @property
    def has_credits(self):
        """Check if user has remaining credits"""
        return self.credits_remaining > 0


class APIKey(models.Model):
    """
    API keys for programmatic access to the platform.
    Supports key rotation and expiration.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='api_keys'
    )
    name = models.CharField(
        max_length=100,
        help_text='Friendly name for this API key'
    )
    key = models.CharField(
        max_length=64,
        unique=True,
        editable=False,
        help_text='The actual API key (shown once on creation)'
    )
    key_prefix = models.CharField(
        max_length=8,
        editable=False,
        help_text='First 8 characters for identification'
    )
    
    # Status
    is_active = models.BooleanField(default=True)
    expires_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='Optional expiration date'
    )
    last_used_at = models.DateTimeField(
        blank=True,
        null=True,
        help_text='Last time this key was used'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'API Key'
        verbose_name_plural = 'API Keys'
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'is_active']),
            models.Index(fields=['key']),
            models.Index(fields=['key_prefix']),
        ]

    def __str__(self):
        return f"{self.name} ({self.key_prefix}...)"

    def save(self, *args, **kwargs):
        if not self.key:
            # Generate a secure random key
            self.key = secrets.token_urlsafe(48)
            self.key_prefix = self.key[:8]
        super().save(*args, **kwargs)

    @classmethod
    def generate_key(cls):
        """Generate a new API key string"""
        return secrets.token_urlsafe(48)


class UsageTracking(models.Model):
    """
    Track API usage metrics per user for billing and rate limiting.
    Records are created daily for each user who makes API calls.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='usage_records'
    )
    date = models.DateField(
        help_text='Date of usage record'
    )
    
    # Request Counts
    compile_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Number of workflow compilations'
    )
    execute_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Number of workflow executions'
    )
    chat_count = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Number of AI chat messages'
    )
    
    # Token/Credit Usage
    tokens_used = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Total LLM tokens consumed'
    )
    credits_used = models.IntegerField(
        default=0,
        validators=[MinValueValidator(0)],
        help_text='Credits consumed this day'
    )
    
    # Cost Tracking
    estimated_cost = models.DecimalField(
        max_digits=10,
        decimal_places=4,
        default=0,
        help_text='Estimated API cost in USD'
    )
    
    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Usage Record'
        verbose_name_plural = 'Usage Records'
        ordering = ['-date']
        unique_together = ['user', 'date']
        indexes = [
            models.Index(fields=['user', '-date']),
            models.Index(fields=['-date']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.date}"

    @property
    def total_requests(self):
        """Total number of API requests for this day"""
        return self.compile_count + self.execute_count + self.chat_count


class PasswordOTP(models.Model):
    """One-time email verification code for password reset/change flows."""

    PURPOSE_PASSWORD_RESET = 'password_reset'
    PURPOSE_PASSWORD_CHANGE = 'password_change'
    PURPOSE_CHOICES = [
        (PURPOSE_PASSWORD_RESET, 'Password Reset'),
        (PURPOSE_PASSWORD_CHANGE, 'Password Change'),
    ]
    MAX_FAILED_ATTEMPTS = 5

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='password_otps'
    )
    purpose = models.CharField(max_length=32, choices=PURPOSE_CHOICES)
    otp_code = models.CharField(max_length=255)
    verification_token = models.CharField(max_length=100, blank=True, null=True, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    is_used = models.BooleanField(default=False)
    failed_attempts = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['user', 'purpose', 'is_used', '-created_at']),
        ]

    def __str__(self):
        return f"{self.user.username} - {self.purpose}"

    def set_otp(self, raw_otp):
        self.otp_code = make_password(raw_otp)

    def check_otp(self, raw_otp):
        return check_password(raw_otp, self.otp_code)

    @property
    def is_expired(self):
        return timezone.now() > self.expires_at

    @property
    def is_locked(self):
        return self.failed_attempts >= self.MAX_FAILED_ATTEMPTS


class UserMemory(models.Model):
    """One durable fact about a user, learned in conversation.

    Personalisation had no substrate before this. `UserProfile` holds tier,
    rate limits and credits; `ChatSession.memory_enabled` only controls whether
    *this conversation's* history is replayed. Nothing anywhere answered "what
    do I know about this person" — so an assistant told to personalise could
    only re-derive it from the current transcript, which is exactly the context
    that gets curated away on a long run and thrown away entirely between
    sessions.

    Deliberately flat text, not a schema. The useful facts are things like "is
    a Django developer", "prefers short answers", "works in IST" — a shape
    nobody can enumerate in advance, and a schema would force every new kind of
    fact through a migration. `category` exists only to group them for display
    and to bound each group.

    Scoped to the user, never to a session: a fact learned in one conversation
    that cannot be recalled in the next is not memory.
    """

    CATEGORIES = [
        ('profile', 'Who they are'),
        ('preference', 'How they like to work'),
        ('project', 'What they are working on'),
        ('context', 'Anything else worth remembering'),
    ]

    #: Facts kept per category. A cap per category rather than one overall,
    #: because a run of new project facts must not push out who the user is —
    #: the categories are not competing for the same purpose. Enforced on
    #: write, oldest-touched first (`core/memory.py`).
    MAX_PER_CATEGORY = 25

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='memories',
    )
    text = models.CharField(max_length=500)
    category = models.CharField(max_length=20, choices=CATEGORIES, default='context')
    #: Where it came from, for a user auditing their own memory. 'agent' means
    #: a model wrote it; 'user' means a person did.
    source = models.CharField(max_length=20, default='agent')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name_plural = 'user memories'
        # A fact stated twice is one fact. The constraint is what lets the
        # write path be an upsert instead of an ever-growing pile of
        # near-duplicates, which is how a memory store becomes unreadable.
        constraints = [
            models.UniqueConstraint(fields=['user', 'text'],
                                    name='unique_user_memory_text'),
        ]
        indexes = [
            models.Index(fields=['user', 'category', '-updated_at']),
        ]
        ordering = ['category', '-updated_at']

    def __str__(self):
        return f'{self.user_id}: {self.text[:60]}'
