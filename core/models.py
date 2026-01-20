from django.db import models
from django.conf import settings
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
