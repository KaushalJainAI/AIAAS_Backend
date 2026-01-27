from django.db import models
from django.conf import settings

class MCPServer(models.Model):
    """
    Configuration for an external MCP Server.
    """
    SERVER_TYPES = (
        ('stdio', 'Standard Input/Output (Subprocess)'),
        ('sse', 'Server-Sent Events (HTTP)'),
    )

    name = models.CharField(max_length=255, unique=True, help_text="Human-readable name for this server")
    type = models.CharField(max_length=10, choices=SERVER_TYPES, default='stdio')
    
    # Stdio Config
    command = models.CharField(max_length=1024, blank=True, null=True, help_text="Executable command (e.g., 'npx', 'python', 'docker')")
    args = models.JSONField(default=list, blank=True, help_text="List of arguments for the command")
    
    # SSE Config
    url = models.URLField(blank=True, null=True, help_text="URL for SSE connection")
    
    # Execution Environment
    env = models.JSONField(default=dict, blank=True, help_text="Environment variables to pass to the server")
    
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    
    # Owner (optional if system-wide)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    def __str__(self):
        return f"{self.name} ({self.type})"
