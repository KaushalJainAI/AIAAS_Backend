from rest_framework import serializers

from .models import MCPServer

#: Commands a user-registered stdio MCP server is allowed to launch. A stdio
#: server is spawned as a subprocess with the user's credentials in its env, so
#: an unrestricted `command` is arbitrary code execution by any authenticated
#: user. These are the standard MCP launchers; a platform operator can seed a
#: server with any other command through a migration (curated servers never pass
#: through this serializer), but the open API surface is held to the launchers.
ALLOWED_STDIO_COMMANDS = frozenset({
    'npx', 'node', 'bunx', 'bun', 'deno',
    'python', 'python3', 'uv', 'uvx', 'pipx',
    'docker',
})


class MCPServerSerializer(serializers.ModelSerializer):
    """
    Serializer for MCPServer.

    `env` is write-only: it may contain sensitive values (legacy non-secret
    vars, but we should not surface them back to any client). Secrets should
    live in the `credentials` app and be wired via `credential_env_map` /
    `credential_header_map` instead of being baked into `env`.
    """

    env = serializers.JSONField(write_only=True, required=False)

    label = serializers.CharField(read_only=True)
    is_system = serializers.BooleanField(read_only=True)
    effective_enabled = serializers.SerializerMethodField()

    class Meta:
        model = MCPServer
        fields = [
            "id",
            "name",
            "type",
            "command",
            "args",
            "url",
            "env",
            "required_credential_types",
            "credential_env_map",
            "credential_header_map",
            "display_name",
            "label",
            "category",
            "tagline",
            "icon_slug",
            "help_url",
            "setup_notes",
            "enabled",
            "effective_enabled",
            "is_system",
            "user",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "user", "created_at", "updated_at"]

    def get_effective_enabled(self, obj) -> bool:
        """
        Whether this server is live *for the requesting user*: the server's own
        `enabled` flag unless the user has an explicit preference row.

        The view supplies `disabled_server_ids` in the serializer context so a
        list of N servers costs one extra query rather than N.
        """
        if not obj.enabled:
            return False
        disabled = self.context.get("disabled_server_ids")
        if disabled is None:
            return True
        return obj.id not in disabled

    def validate(self, attrs):
        server_type = attrs.get("type") or (self.instance.type if self.instance else "stdio")
        if server_type == "stdio":
            command = attrs.get("command") if "command" in attrs else (self.instance.command if self.instance else None)
            if not command:
                raise serializers.ValidationError({"command": "Required for stdio servers."})
            # Match on the bare program name so an absolute path or an argument
            # string can't smuggle a disallowed binary past the check.
            program = str(command).strip().split()[0]
            program = program.replace("\\", "/").rsplit("/", 1)[-1]
            if program not in ALLOWED_STDIO_COMMANDS:
                raise serializers.ValidationError({
                    "command": (
                        f"'{program}' is not an allowed launcher. "
                        f"Allowed: {', '.join(sorted(ALLOWED_STDIO_COMMANDS))}."
                    )
                })
        elif server_type == "sse":
            url = attrs.get("url") if "url" in attrs else (self.instance.url if self.instance else None)
            if not url:
                raise serializers.ValidationError({"url": "Required for SSE servers."})
            # A user-supplied SSE URL is an SSRF vector: it must not resolve to a
            # private/link-local address (EC2 metadata, internal services).
            from core.safety.net import validate_url
            is_safe, reason = validate_url(url)
            if not is_safe:
                raise serializers.ValidationError({"url": reason})
        return attrs
