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
    # How hard the model is asked to think, from `llm.effort.LADDER`. Blank is
    # the model's own default and is what every existing session keeps, so
    # turning the knob on changes nothing until someone chooses. Stored beside
    # the model rather than derived from it because it is a preference, not a
    # capability: two users on the same model want different answers here.
    llm_effort = models.CharField(max_length=10, blank=True, default='')
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


class VisionExchange(models.Model):
    """
    One question the main agent asked a vision model about an attachment, and
    the answer it got back.

    Doing three jobs at once: it is the witness's memory (a follow-up arrives
    with the earlier exchanges already in context), the audit trail of what was
    asked on the user's behalf, and the data behind the UI affordance that shows
    a user the assistant did not see their image — it questioned something that
    could.
    """

    session = models.ForeignKey(
        ChatSession, on_delete=models.CASCADE, related_name='vision_exchanges'
    )
    attachment = models.ForeignKey(
        ChatAttachment, on_delete=models.CASCADE, related_name='vision_exchanges'
    )
    question = models.TextField()
    answer = models.TextField()
    model = models.CharField(max_length=120, blank=True, default="")
    #: Set when the VLM and the parser read the same glyphs differently. The
    #: readings themselves stay in `answer`; this is the flag a UI can filter on.
    disagreement = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['session', 'attachment', 'created_at']),
        ]

    def __str__(self):
        return f"vision Q on {self.attachment_id}: {self.question[:40]}"


def _tool_output_id() -> str:
    """Short, quotable handle. The model has to copy it back verbatim."""
    return uuid.uuid4().hex[:12]


class ToolOutput(models.Model):
    """
    The complete text of a tool result that was too large to replay to the model.

    A tool that returns more than `TOOL_OUTPUT_CHAR_LIMIT` used to have its
    result dropped into the transcript whole, where it either ate the context
    window or was blindly trimmed by `llm.clamp_input` — which trims from the
    middle and tells nobody. The agent could not distinguish "the page said
    nothing more" from "we threw the rest away".

    So the bounded preview stays in the transcript as the durable record, and
    the full text lands here where `read_tool_output` can page through it. That
    is the same trade the per-tool budgets already make, moved to one place and
    made recoverable: the model is told what it is missing and how to go get it,
    instead of being handed a silent stump.

    Not a `ChatSession` foreign key on purpose. `tools_node` is shared with the
    agent runtime, whose `session_id` identifies an agent run rather than a chat,
    so the scope is stored as an opaque key and checked as one.
    """

    id = models.CharField(
        primary_key=True, max_length=12, default=_tool_output_id, editable=False
    )
    #: Null for guest chat, which has no account behind it. A null owner is not
    #: a wildcard: `read_tool_output` matches on it exactly, so one guest cannot
    #: read another's spill by guessing an id.
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='tool_outputs', null=True, blank=True,
    )
    #: `TurnContext.session_id` — a chat session id or an agent run id.
    session_key = models.CharField(max_length=64, db_index=True)
    turn_id = models.CharField(max_length=64, blank=True, default="")
    tool_name = models.CharField(max_length=160)
    content = models.TextField()
    total_chars = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    #: Retention, not correctness. Once this passes the row is collectable and
    #: `read_tool_output` reports the text as expired rather than pretending the
    #: tool returned nothing.
    expires_at = models.DateTimeField(db_index=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['session_key', 'created_at']),
        ]

    def __str__(self):
        return f"{self.tool_name} output {self.id} ({self.total_chars} chars)"


class ToolPermission(models.Model):
    """
    A standing "always allow" the user gave for one tool.

    Without it the per-call gate in `chat.permissions` is unusable in practice:
    a connector the user reaches for twenty times a day would prompt twenty
    times, and the twenty-first prompt gets approved without being read. A
    remembered decision is how a gate stays meaningful — the prompts that
    remain are the ones the user has not already thought about.

    `session_key` empty means the decision stands for every conversation;
    otherwise it is scoped to the one the user made it in. Nothing here records
    a *denial*: refusing is already expressed by not approving, and a stored
    "never" would need its own expiry and its own way to be taken back.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='tool_permissions',
    )
    tool_name = models.CharField(max_length=160)
    #: Empty for "in every conversation"; a `TurnContext.session_id` otherwise.
    session_key = models.CharField(max_length=64, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        constraints = [
            models.UniqueConstraint(
                fields=['user', 'tool_name', 'session_key'],
                name='unique_tool_permission_scope',
            ),
        ]
        indexes = [
            models.Index(fields=['user', 'tool_name']),
        ]

    def __str__(self):
        scope = self.session_key or 'all conversations'
        return f"{self.user_id} allows {self.tool_name} ({scope})"
