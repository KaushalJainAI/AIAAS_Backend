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
    # Wide enough for the long ratios the catalog advertises, e.g. '19.5:9'.
    aspect_ratio = models.CharField(max_length=16, blank=True, null=True)
    # The explicit alternative to a resolution tier: 'WIDTHxHEIGHT'. Both the
    # image and video endpoints accept it, and a model that advertises
    # `supported_sizes` is one where the tier list may be empty — so without
    # this there is no way to say how big the output should be at all.
    size = models.CharField(max_length=20, blank=True, null=True)
    duration = models.CharField(max_length=10, blank=True, null=True)
    seed = models.BigIntegerField(blank=True, null=True)
    # Image-to-image / style guidance: `input_references` on both endpoints.
    # HTTP(S) urls or data URIs, capped per model by `max_references`. Stored
    # as sent, because a result is only reproducible with the inputs that made
    # it.
    reference_urls = models.JSONField(default=list, blank=True)

    # Image specific
    quality = models.CharField(max_length=20, blank=True, null=True)
    output_format = models.CharField(max_length=10, blank=True, null=True)
    #: `auto` | `transparent` | `opaque`, on the models that advertise it.
    background = models.CharField(max_length=20, blank=True, null=True)
    #: 0-100, webp/jpeg only.
    output_compression = models.IntegerField(blank=True, null=True)
    #: `n` — an upper bound on how many images to return, not a promise. One
    #: row still describes one request; the extra images land in `output_urls`.
    batch_size = models.IntegerField(blank=True, null=True)

    # Video specific. `motion_intensity` and `fps` are retained only because
    # historical rows carry values; OpenRouter's video API accepts neither, so
    # nothing writes them and the UI no longer offers them.
    motion_intensity = models.IntegerField(blank=True, null=True)
    fps = models.IntegerField(blank=True, null=True)
    generate_audio = models.BooleanField(blank=True, null=True)

    # Audio specific
    voice = models.CharField(max_length=50, blank=True, null=True)
    speed = models.FloatField(blank=True, null=True)
    #: Tone direction for the OpenAI speech family ("speak warmly, unhurried").
    instructions = models.TextField(blank=True, null=True)
    #: `mp3` | `pcm`. The endpoint defaults to pcm; we ask for mp3 unless told
    #: otherwise, because that is what an <audio> element can play.
    response_format = models.CharField(max_length=10, blank=True, null=True)

    #: Frame pinning for video: `[{"url": ..., "frame_type": "first_frame"}]`.
    #: Which slots a model accepts comes from its `frame_slots`.
    frame_images = models.JSONField(default=list, blank=True)

    output_url = models.TextField(blank=True, null=True) # Changed to TextField for base64
    #: Every output of the request, in order. `output_url` stays the first one
    #: so nothing that reads a single result had to change; a batch of four is
    #: one request, one row, four entries here.
    output_urls = models.JSONField(default=list, blank=True)
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
