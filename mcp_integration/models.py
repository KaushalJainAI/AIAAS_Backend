from django.db import models
from django.conf import settings


class MCPServer(models.Model):
    """
    Configuration for an external MCP Server.

    Credential injection:
      - For `stdio` servers, secrets are injected into the subprocess env via `credential_env_map`.
      - For `sse` servers, secrets are injected into HTTP headers via `credential_header_map`.

    Mapping syntax (both maps):
      {"<target_key>": "<credential_type_slug>:<field_name>"}

    For `credential_header_map`, the value may also be a literal string containing
    `{<credential_type_slug>:<field_name>}` placeholders (useful for Bearer tokens).
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

    # Execution Environment (non-secret env vars; secrets come via credentials)
    env = models.JSONField(default=dict, blank=True, help_text="Non-secret environment variables to pass to the server")

    # Credential wiring
    required_credential_types = models.JSONField(
        default=list,
        blank=True,
        help_text="List of CredentialType slugs this server requires (e.g., ['github_token'])"
    )
    credential_env_map = models.JSONField(
        default=dict,
        blank=True,
        help_text='Maps env var name -> "<credential_slug>:<field>". Used for stdio.'
    )
    credential_header_map = models.JSONField(
        default=dict,
        blank=True,
        help_text='Maps HTTP header name -> value (may contain {slug:field} placeholders). Used for SSE.'
    )

    setup_notes = models.TextField(blank=True, help_text="Human-readable setup notes shown in the UI")

    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Owner (NULL = system-wide, visible to all users)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['enabled', 'user']),
        ]

    def __str__(self):
        return f"{self.name} ({self.type})"
