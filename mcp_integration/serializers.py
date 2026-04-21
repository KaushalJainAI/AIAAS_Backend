from rest_framework import serializers

from .models import MCPServer


class MCPServerSerializer(serializers.ModelSerializer):
    """
    Serializer for MCPServer.

    `env` is write-only: it may contain sensitive values (legacy non-secret
    vars, but we should not surface them back to any client). Secrets should
    live in the `credentials` app and be wired via `credential_env_map` /
    `credential_header_map` instead of being baked into `env`.
    """

    env = serializers.JSONField(write_only=True, required=False)

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
            "setup_notes",
            "enabled",
            "user",
            "created_at",
            "updated_at",
        ]
        read_only_fields = ["id", "user", "created_at", "updated_at"]

    def validate(self, attrs):
        server_type = attrs.get("type") or (self.instance.type if self.instance else "stdio")
        if server_type == "stdio":
            command = attrs.get("command") if "command" in attrs else (self.instance.command if self.instance else None)
            if not command:
                raise serializers.ValidationError({"command": "Required for stdio servers."})
        elif server_type == "sse":
            url = attrs.get("url") if "url" in attrs else (self.instance.url if self.instance else None)
            if not url:
                raise serializers.ValidationError({"url": "Required for SSE servers."})
        return attrs
