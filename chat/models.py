from django.db import models
from django.conf import settings
import uuid

class ChatSession(models.Model):
    """
    A standalone chat session, independent of the workflows.
    Ensures per-chat LLM configuration.
    """
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.CASCADE, 
        related_name='chat_sessions'
    )
    
    title = models.CharField(max_length=255, default="New Chat", blank=True)
    
    # Per-conversation AI Settings
    llm_provider = models.CharField(max_length=50, default='openrouter')
    llm_model = models.CharField(max_length=100, default='google/gemini-2.0-flash-exp:free')
    system_prompt = models.TextField(blank=True, default="")
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    class Meta:
        ordering = ['-updated_at']
        
    def __str__(self):
        return f"{self.title} ({self.id})"

class ChatMessage(models.Model):
    """
    Messages within a Standalone Chat Session.
    """
    ROLE_CHOICES = [
        ('user', 'User'),
        ('assistant', 'Assistant'),
        ('system', 'System'),
    ]
    
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    
    # Store things like image URLs, search tool calls, token usage here
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self):
        return f"{self.role}: {self.content[:30]}"
