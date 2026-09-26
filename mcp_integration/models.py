from django.db import models
from django.conf import settings


class MCPServer(models.Model):
    """
    Configuration for an external MCP Server.

    Credential injection:
      - For `stdio` servers, secrets are injected into the subprocess env via `credential_env_map`.
      - For remote servers (`http`, `sse`), secrets are injected into HTTP
        headers via `credential_header_map`.

    Mapping syntax (both maps):
      {"<target_key>": "<credential_type_slug>:<field_name>"}

    For `credential_header_map`, the value may also be a literal string containing
    `{<credential_type_slug>:<field_name>}` placeholders (useful for Bearer tokens).
    """
    #: How we talk to the server.
    #:
    #: `http` is MCP's **streamable HTTP** transport and is what every hosted
    #: connector now speaks (`https://mcp.notion.com/mcp`,
    #: `https://mcp.slack.com/mcp`). `sse` is its deprecated predecessor, kept
    #: because rows may still point at an older endpoint; new remote rows should
    #: be `http`. Both are URL-based and share `url` / `credential_header_map`,
    #: so moving a row between them is a one-column change.
    #:
    #: `native` is not a transport: the card's tools are registered built-ins
    #: that call the vendor's REST API from this process (`chat/tools/google/`),
    #: matched to the row by `icon_slug`. The row exists so the Connections
    #: switch, the credential and an agent's `connectors` scope keep governing
    #: the connector. Curated only — see `mcp_integration/native.py`.
    SERVER_TYPES = (
        ('stdio', 'Standard Input/Output (Subprocess)'),
        ('http', 'Streamable HTTP'),
        ('sse', 'Server-Sent Events (HTTP, deprecated)'),
        ('native', 'Native (built-in tools)'),
    )

    #: The two transports that reach a server over the network rather than by
    #: spawning it. Anything in here needs `url` and gets `assert_url_safe` at
    #: connect time; anything not in here needs `command`.
    REMOTE_TYPES = frozenset({'http', 'sse'})

    name = models.CharField(max_length=255, help_text="Human-readable name for this server")
    type = models.CharField(max_length=10, choices=SERVER_TYPES, default='stdio')

    # Stdio Config
    command = models.CharField(max_length=1024, blank=True, null=True, help_text="Executable command (e.g., 'npx', 'python', 'docker')")
    args = models.JSONField(default=list, blank=True, help_text="List of arguments for the command")

    # Remote config (http / sse)
    url = models.URLField(
        blank=True, null=True,
        help_text="Endpoint for a remote server (streamable HTTP or SSE)",
    )

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
        help_text='Maps HTTP header name -> value (may contain {slug:field} placeholders). Used for http/sse.'
    )

    credential_file_map = models.JSONField(
        default=dict,
        blank=True,
        help_text=(
            'Maps env var name -> {"filename", "target", "content"}. The '
            'content is rendered with the same {slug:field} placeholders as '
            'credential_header_map and written to a per-user temp file; the '
            'env var receives its path (target="file") or its directory '
            '(target="dir"). For servers that read credentials from disk.'
        ),
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

    #: Announced, not yet shippable. A *presentation* flag only: it never
    #: widens access, so it is always paired with `enabled=False`, which is
    #: what actually withholds the tools (`_visible_servers_queryset` filters
    #: on `enabled`, and the toggle endpoint answers 409 for a platform-off
    #: row). It exists because "Unavailable" and "Coming soon" need opposite
    #: readings -- the first says something is broken, the second that it is
    #: on the way -- and the UI must not tell them apart by connector name,
    #: which is metadata, not code.
    coming_soon = models.BooleanField(
        default=False,
        help_text=(
            "Show as an upcoming feature. Pair with enabled=False; "
            "this flag alone grants nothing."
        ),
    )
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


# ---------------------------------------------------------------------------
# OAuth for remote (http/sse) MCP servers
# ---------------------------------------------------------------------------
#
# Hosted MCP servers authenticate with OAuth rather than a pasted token, and
# they advertise everything needed to do it: `mcp.notion.com` publishes an
# authorization-server document with a `registration_endpoint`, so the backend
# registers *itself* (RFC 7591 Dynamic Client Registration) and no deployment
# has to create a Notion app or carry a client secret.
#
# Three rows rather than one, because they have three different lifetimes:
#   MCPOAuthClient — per authorization server. Registered once, shared by users.
#   MCPOAuthFlow   — per authorization attempt. Seconds to minutes, then gone.
#   MCPOAuthToken  — per (user, server). The thing that actually grants access.


class MCPOAuthClient(models.Model):
    """A dynamically-registered OAuth client for one authorization server.

    Keyed on `(issuer, redirect_uri)` because a registration is only valid for
    the redirect URIs it was registered with — a deployment reachable at two
    origins legitimately needs two clients.

    `client_secret` is nullable and usually absent: these servers accept
    `token_endpoint_auth_method: "none"`, so the client is public and PKCE is
    what protects the exchange. When a server does issue one it is encrypted
    with the same key as every other secret in the platform.
    """

    issuer = models.URLField(help_text="Authorization server issuer URL")
    redirect_uri = models.URLField(help_text="Redirect URI this client was registered for")
    client_id = models.CharField(max_length=255)
    client_secret = models.BinaryField(
        null=True, blank=True,
        help_text="Fernet-encrypted client secret, when the server issues one",
    )
    #: The discovered authorization-server metadata, kept so a flow does not
    #: have to re-fetch `.well-known` on every authorize.
    metadata = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('issuer', 'redirect_uri')]

    def __str__(self):
        return f"{self.issuer} client {self.client_id}"


class MCPOAuthFlow(models.Model):
    """One in-flight authorization, holding its PKCE verifier.

    The verifier lives here rather than in the `state` parameter on purpose.
    State travels to the authorization server and back through the browser
    alongside the code, so a verifier carried in it would be interceptable by
    anything that could intercept the code — which is precisely what PKCE
    exists to defeat. It is not in the cache either: the default cache is
    per-process, and the callback need not land on the worker that started the
    flow.

    Rows are consumed on use and swept by age; `is_expired` is the only reader
    of `created_at`.
    """

    #: Random, opaque, and the only thing that travels in the URL.
    state = models.CharField(max_length=128, unique=True, db_index=True)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE)
    client = models.ForeignKey(MCPOAuthClient, on_delete=models.CASCADE)
    code_verifier = models.CharField(max_length=255)
    redirect_uri = models.URLField()
    created_at = models.DateTimeField(auto_now_add=True)

    #: How long an unfinished authorization stays usable.
    TTL_SECONDS = 600

    def is_expired(self) -> bool:
        from django.utils import timezone
        from datetime import timedelta
        return timezone.now() > self.created_at + timedelta(seconds=self.TTL_SECONDS)

    def __str__(self):
        return f"oauth flow {self.state[:8]}… for server {self.server_id}"


class MCPOAuthToken(models.Model):
    """What a user's completed authorization grants, per server.

    Deliberately not a `credentials.Credential`: that model's refresh path is
    Google-only by design (it exchanges with `settings.GOOGLE_OAUTH_CLIENT_ID`
    and refuses every other slug), and reusing it would have meant either
    generalising that hot, security-sensitive method or inventing a
    `CredentialType` per MCP server. Tokens here refresh against the issuer
    their own flow discovered.
    """

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name='mcp_oauth_tokens',
    )
    server = models.ForeignKey(
        MCPServer, on_delete=models.CASCADE, related_name='oauth_tokens',
    )
    client = models.ForeignKey(MCPOAuthClient, on_delete=models.CASCADE)

    access_token = models.BinaryField(help_text="Fernet-encrypted access token")
    refresh_token = models.BinaryField(
        null=True, blank=True, help_text="Fernet-encrypted refresh token",
    )
    #: Null means the server did not say. Treated as "does not expire" rather
    #: than "already expired": refusing to send a token the server never said
    #: anything about would break every server that issues non-expiring ones.
    expires_at = models.DateTimeField(null=True, blank=True)
    scope = models.CharField(max_length=512, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'server')]
        indexes = [models.Index(fields=['user', 'server'])]

    def __str__(self):
        return f"oauth token for user {self.user_id} on server {self.server_id}"


class MCPToolCatalogue(models.Model):
    """The last good `list_tools` answer for a connection, kept durably.

    The Redis cache had nothing underneath it, so the fallback chain was
    **Redis -> nothing**. A restart, an eviction, a deploy, the 24h hard TTL
    lapsing, or a user's first turn all landed on the same cliff: a cold `npx`
    start (~21s, cut off at `AGENT_LIST_TOOLS_TIMEOUT`) in front of the first
    token, and a turn that ran with *no connector tools at all* — slower and
    less capable at once, with nothing able to say why.

    A tool list is neither secret nor volatile: it changes when someone edits a
    connection, and that path already invalidates explicitly. There was never a
    reason for it to live only somewhere that evicts. With this row the chain is
    **Redis -> database -> live handshake**, so a cache miss costs one indexed
    read and still yields a full toolbox.

    Keyed `(server, user)` to mirror the Redis key exactly, so adding this tier
    changes no semantics — `user` is null for a system-level listing, the same
    case `_coerce_user_id` already returns None for.

    Open question, deliberately not assumed: whether `list_tools` really
    differs per user, or whether credentials only decide whether a *call*
    succeeds. If it turns out to be server-level, a null-user row becomes a
    shared baseline that a user with no row of their own can fall back to, and
    the constraints below already allow exactly one such row per server.
    """

    server = models.ForeignKey(
        MCPServer, on_delete=models.CASCADE, related_name='tool_catalogues',
    )
    #: Null means a system-level listing, not "everyone".
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.CASCADE, related_name='mcp_tool_catalogues',
    )
    tools = models.JSONField(default=list)
    listed_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            # Two constraints rather than one `unique_together`, because SQL
            # treats NULLs as distinct: `unique_together('server', 'user')`
            # would happily allow a hundred rows with user=NULL for one server,
            # which is precisely the row that has to be unique for a shared
            # baseline to mean anything.
            models.UniqueConstraint(
                fields=['server', 'user'],
                condition=models.Q(user__isnull=False),
                name='uniq_mcp_catalogue_per_user',
            ),
            models.UniqueConstraint(
                fields=['server'],
                condition=models.Q(user__isnull=True),
                name='uniq_mcp_catalogue_system',
            ),
        ]
        indexes = [models.Index(fields=['server', 'user'])]

    def __str__(self):
        return f"{len(self.tools or [])} tools for server {self.server_id}"


class MCPToolPin(models.Model):
    """What one third-party tool looked like when it was trusted.

    Defence against MCP **tool poisoning** and the **rug pull** (a server
    changing a tool after the user connected it): a tool's name, description
    and input schema reach the model verbatim, so a description is a prompt
    written by a third party. The pin is a sha256 of those three, taken on
    first sight. See `mcp_integration/pinning.py` for the rules; in short, a
    tool whose description is addressed to an AI starts `quarantined`, a tool
    whose digest later changes becomes `changed`, and neither is offered to a
    model or dispatched until the user approves it on Connections.

    Per user, not per server: approving a change is a person's decision about
    their own account, and a curated row is shared by everyone.
    """

    STATUS_CHOICES = [
        ('trusted', 'Trusted'),
        ('changed', 'Changed since approved'),
        ('quarantined', 'Held for review'),
    ]

    server = models.ForeignKey(MCPServer, on_delete=models.CASCADE, related_name='tool_pins')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name='mcp_tool_pins')
    tool_name = models.CharField(max_length=255)
    #: The digest the user trusts. Empty for a tool quarantined on first sight.
    digest = models.CharField(max_length=64, blank=True, default='')
    #: The digest seen now, when it differs from `digest`.
    pending_digest = models.CharField(max_length=64, blank=True, default='')
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='trusted')
    #: Why it is held, in a sentence for the Connections page.
    reason = models.CharField(max_length=300, blank=True, default='')
    #: The description as it is now, so the user reads what they approve.
    description = models.TextField(blank=True, default='')
    first_seen = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=['server', 'user', 'tool_name'],
                                    name='uniq_mcp_tool_pin'),
        ]
        indexes = [models.Index(fields=['server', 'user'])]

    def __str__(self):
        return f"{self.tool_name} on {self.server_id}: {self.status}"
