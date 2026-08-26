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

    name = models.CharField(max_length=255, help_text="Human-readable name for this server")
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

    # ---- Presentation metadata ------------------------------------------
    # This lives in the database, not the frontend, so adding a connector is a
    # fixture row rather than a code change. The UI keys icons off `icon_slug`
    # (a stable identifier) instead of off `name` (user-facing copy that
    # changes), which is what previously made new servers render untitled and
    # icon-less until someone edited the React source.
    CATEGORY_CHOICES = (
        ('google_workspace', 'Google Workspace'),
        ('communication', 'Communication'),
        ('productivity', 'Productivity'),
        ('development', 'Development'),
        ('utilities', 'Utilities'),
        ('custom', 'Custom'),
    )

    display_name = models.CharField(
        max_length=255,
        blank=True,
        help_text="Name shown in the UI. Falls back to `name` when blank."
    )
    category = models.CharField(
        max_length=32,
        choices=CATEGORY_CHOICES,
        default='custom',
        help_text="Grouping used by the Connections page"
    )
    tagline = models.CharField(
        max_length=255,
        blank=True,
        help_text="One-line plain-language description of what this connection enables"
    )
    icon_slug = models.CharField(
        max_length=64,
        blank=True,
        help_text="Stable icon identifier the frontend maps to an icon component (e.g. 'gmail')"
    )
    help_url = models.URLField(
        blank=True,
        help_text="Link to provider docs for obtaining credentials"
    )

    setup_notes = models.TextField(blank=True, help_text="Human-readable setup notes shown in the UI")

    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    # Owner (NULL = system-wide, visible to all users)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, null=True, blank=True)

    class Meta:
        unique_together = [('name', 'user')]
        indexes = [
            models.Index(fields=['enabled', 'user']),
        ]

    def __str__(self):
        return f"{self.name} ({self.type})"

    @property
    def label(self) -> str:
        """The name to show a user."""
        return self.display_name or self.name

    @property
    def is_system(self) -> bool:
        return self.user_id is None


class MCPServerPreference(models.Model):
    """
    Per-user enable/disable state for a server.

    System-wide servers (`MCPServer.user IS NULL`) are shared read-only
    templates: one user turning Gmail off must not turn it off for everyone, so
    it cannot be expressed by flipping `MCPServer.enabled`. Attempting that was
    the source of `PATCH /api/mcp/servers/<id>/ -> 403` — the UI offered a
    toggle the API was right to refuse. The toggle now writes a row here.

    A missing row means "inherit `MCPServer.enabled`", so this table only ever
    holds explicit user choices and an untouched account has none.
    """
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='mcp_server_preferences',
    )
    server = models.ForeignKey(
        MCPServer,
        on_delete=models.CASCADE,
        related_name='preferences',
    )
    enabled = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'server')]
        indexes = [
            models.Index(fields=['user', 'enabled']),
        ]

    def __str__(self):
        state = 'enabled' if self.enabled else 'disabled'
        return f"{self.server_id} {state} for user {self.user_id}"
