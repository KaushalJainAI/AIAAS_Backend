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
    
    # Per-conversation AI Settings.
    # Default to NVIDIA NIM (backed by a platform NVIDIA_API_KEY) so new chats
    # work out of the box; users can switch provider/model per conversation.
    llm_provider = models.CharField(max_length=50, default='nvidia')
    llm_model = models.CharField(max_length=100, default='nvidia/nemotron-3-super-120b-a12b')
    intent = models.CharField(max_length=50, default='chat')
    system_prompt = models.TextField(blank=True, default="")

    # When off, the assistant answers from the current message alone: no earlier
    # turns are replayed and the history-search tool is withheld.
    #
    # This gates *recall*, not *retention* — messages are still written to the DB
    # exactly as before. That distinction matters: a user who turns this off to
    # ask a one-off question expects the conversation to still be there when they
    # turn it back on. Purging on toggle would be a different, destructive feature
    # and is deliberately not what this does.
    memory_enabled = models.BooleanField(default=True)

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
