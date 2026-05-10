from django.db import models
from django.conf import settings

class Generation(models.Model):
    TYPES = [
        ('image', 'Image'),
        ('video', 'Video'),
        ('audio', 'Audio'),
    ]
    
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('processing', 'Processing'),
        ('completed', 'Completed'),
        ('failed', 'Failed'),
    ]

    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name='generations')
    type = models.CharField(max_length=10, choices=TYPES)
    prompt = models.TextField()
    negative_prompt = models.TextField(blank=True, null=True)
    model = models.CharField(max_length=100)
    
    # Common parameters
    resolution = models.CharField(max_length=20, blank=True, null=True)
    aspect_ratio = models.CharField(max_length=10, blank=True, null=True)
    duration = models.CharField(max_length=10, blank=True, null=True)
    seed = models.BigIntegerField(blank=True, null=True)
    
    # Video specific
    motion_intensity = models.IntegerField(blank=True, null=True)
    fps = models.IntegerField(blank=True, null=True)
    
    # Audio specific
    voice = models.CharField(max_length=50, blank=True, null=True)
    speed = models.FloatField(blank=True, null=True)
    
    output_url = models.TextField(blank=True, null=True) # Changed to TextField for base64
    job_id = models.CharField(max_length=255, blank=True, null=True)
    polling_url = models.URLField(max_length=1000, blank=True, null=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    error_message = models.TextField(blank=True, null=True)
    
    # Metadata for dynamic options
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.type} - {self.prompt[:30]}..."


class ImagineConversation(models.Model):
    STATUS_CHOICES = [
        ('idle', 'Idle'),
        ('awaiting_hitl', 'Awaiting HITL'),
        ('generating', 'Generating'),
    ]

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='imagine_conversations',
    )
    title = models.CharField(max_length=120, blank=True, default='')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='idle')
    pending_intent = models.JSONField(blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-updated_at']

    def __str__(self):
        return self.title or f"Conversation {self.id}"


class ImagineMessage(models.Model):
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ]

    conversation = models.ForeignKey(
        ImagineConversation,
        on_delete=models.CASCADE,
        related_name='messages',
    )
    role = models.CharField(max_length=12, choices=ROLE_CHOICES)
    content = models.TextField(blank=True, default='')
    intent = models.JSONField(blank=True, null=True)
    generation = models.ForeignKey(
        Generation,
        on_delete=models.SET_NULL,
        blank=True,
        null=True,
        related_name='messages',
    )
    requires_hitl = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
