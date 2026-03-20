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
    intent = models.CharField(max_length=50, default='chat')
    system_prompt = models.TextField(blank=True, default="")
    
    # Token usage tracking
    total_tokens_used = models.IntegerField(default=0)
    
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
    
    MESSAGE_TYPE_CHOICES = [
        ('chat', 'Chat'),
        ('search', 'Search'),
        ('image', 'Image Generation'),
        ('video', 'Video Generation'),
        ('coding', 'Coding'),
        ('file_manipulation', 'File Manipulation'),
        ('workflow_suggestion', 'Workflow Suggestion'),
        ('workflow_result', 'Workflow Result'),
        ('system', 'System'),
    ]
    
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='messages')
    role = models.CharField(max_length=20, choices=ROLE_CHOICES)
    content = models.TextField()
    message_type = models.CharField(max_length=30, choices=MESSAGE_TYPE_CHOICES, default='chat')
    
    # Stores: citations, follow_ups, search_results, image_url, workflow_id, token_count, etc.
    metadata = models.JSONField(default=dict, blank=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['session', 'created_at']),
        ]

    def __str__(self):
        return f"{self.role}: {self.content[:30]}"


class ChatAttachment(models.Model):
    """
    File attachments uploaded to a chat session (images, PDFs, PPTs, etc.)
    """
    ATTACHMENT_TYPE_CHOICES = [
        ('image', 'Image'),
        ('pdf', 'PDF'),
        ('pptx', 'PowerPoint'),
        ('text', 'Text File'),
        ('other', 'Other'),
    ]
    
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    session = models.ForeignKey(ChatSession, on_delete=models.CASCADE, related_name='attachments')
    message = models.ForeignKey(
        ChatMessage, on_delete=models.SET_NULL, 
        null=True, blank=True, related_name='attachments'
    )
    
    file = models.FileField(upload_to='chat_attachments/%Y/%m/')
    filename = models.CharField(max_length=255)
    file_type = models.CharField(max_length=20, choices=ATTACHMENT_TYPE_CHOICES, default='other')
    file_size = models.IntegerField(default=0)  # bytes
    
    # Extracted text content for PDFs/PPTs/text files
    extracted_text = models.TextField(blank=True, default="")
    
    # Hierarchical RAG Support
    inference_document = models.ForeignKey(
        'inference.Document', on_delete=models.SET_NULL, 
        null=True, blank=True, related_name='chat_attachments'
    )
    is_large_file = models.BooleanField(default=False)
    
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.filename} ({self.file_type})"
