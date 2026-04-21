"""
CredentialInjector — the single point of truth for resolving MCP server
credentials from the `credentials` app and materialising them into the shape
an MCP server process (stdio) or HTTP client (SSE) expects.

All MCP code paths — client manager, workflow validator, tool provider — go
through this module. Do not re-implement credential resolution elsewhere.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

from asgiref.sync import sync_to_async

from .models import MCPServer

logger = logging.getLogger(__name__)


# Matches `{slug:field}` placeholders inside header values.
_PLACEHOLDER_RE = re.compile(r"\{([a-zA-Z0-9_\-]+):([a-zA-Z0-9_\-.]+)\}")

# Matches `slug:field` used as a whole value (env var map format).
_MAPPING_RE = re.compile(r"^([a-zA-Z0-9_\-]+):([a-zA-Z0-9_\-.]+)$")


class CredentialMissingError(Exception):
    """User has not configured a credential that this MCP server requires."""

    def __init__(self, slug: str, server_name: str):
        self.slug = slug
        self.server_name = server_name
        super().__init__(
            f"MCP server '{server_name}' requires a '{slug}' credential. "
            f"Add one under Settings → Credentials."
        )


class CredentialInvalidError(Exception):
    """A credential is present but cannot be decrypted or is missing a field."""


@dataclass
class ResolvedCredentials:
    """Materialised credential values keyed by the server's mapping target."""
    env_vars: dict[str, str]
    headers: dict[str, str]
    used_credential_ids: list[int]


def _lookup_credential_sync(user_id: int, slug: str):
    from credentials.models import Credential

    qs = Credential.objects.filter(
        user_id=user_id,
        credential_type__slug=slug,
        is_active=True,
    ).select_related("credential_type").order_by("-is_verified", "-updated_at")
    return qs.first()


async def _resolve_slug(user_id: int, slug: str, server_name: str):
    cred = await sync_to_async(_lookup_credential_sync)(user_id, slug)
    if cred is None:
        raise CredentialMissingError(slug, server_name)
    return cred


def _decrypt(cred) -> dict[str, Any]:
    try:
        return cred.get_credential_data()
    except Exception as e:  # noqa: BLE001
        raise CredentialInvalidError(
            f"Failed to decrypt credential '{cred.name}' (id={cred.id}): {type(e).__name__}"
        ) from e


def _extract_field(data: dict[str, Any], field_path: str) -> Any:
    cur: Any = data
    for part in field_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _coerce_user_id(user) -> int | None:
    """Accept a User instance or a plain user_id int."""
    if user is None:
        return None
    if isinstance(user, int):
        return user
    return getattr(user, "id", None)


class CredentialInjector:
    """
    Resolves MCP server credential requirements from the user's credential vault.

    Accepts either a User object or a plain user_id int for convenience.
    """

    @staticmethod
    async def resolve(server: MCPServer, user) -> ResolvedCredentials:
        """
        Load every credential referenced by the server's mappings and return
        concrete env vars + headers ready to inject.

        Raises CredentialMissingError if a required credential is absent,
        CredentialInvalidError if decryption fails.
        """
        user_id = _coerce_user_id(user)
        if user_id is None:
            # System-level execution (no user context). Nothing to inject.
            return ResolvedCredentials(env_vars={}, headers={}, used_credential_ids=[])

        server_name = server.name
        cred_cache: dict[str, tuple[int, dict[str, Any]]] = {}

        async def _get(slug: str) -> tuple[int, dict[str, Any]]:
            if slug not in cred_cache:
                cred = await _resolve_slug(user_id, slug, server_name)
                cred_cache[slug] = (cred.id, _decrypt(cred))
            return cred_cache[slug]

        # Required types must exist regardless of whether they're mapped.
        for slug in server.required_credential_types or []:
            await _get(slug)

        env_vars: dict[str, str] = {}
        for env_key, mapping in (server.credential_env_map or {}).items():
            if not isinstance(mapping, str):
                logger.warning("Skipping non-string mapping %r on server %s", mapping, server_name)
                continue
            match = _MAPPING_RE.match(mapping)
            if not match:
                logger.warning("Invalid env mapping %r on server %s", mapping, server_name)
                continue
            slug, field_path = match.group(1), match.group(2)
            _, data = await _get(slug)
            value = _extract_field(data, field_path)
            if value is None:
                raise CredentialInvalidError(
                    f"Credential '{slug}' is missing field '{field_path}' "
                    f"(needed for env var {env_key} on {server_name})"
                )
            env_vars[env_key] = str(value)

        headers: dict[str, str] = {}
        for header_key, template in (server.credential_header_map or {}).items():
            if not isinstance(template, str):
                continue

            placeholders = list(_PLACEHOLDER_RE.finditer(template))
            if not placeholders:
                headers[header_key] = template
                continue

            resolved: dict[str, str] = {}
            for p in placeholders:
                slug, field_path = p.group(1), p.group(2)
                key = f"{slug}:{field_path}"
                if key in resolved:
                    continue
                _, data = await _get(slug)
                value = _extract_field(data, field_path)
                if value is None:
                    raise CredentialInvalidError(
                        f"Credential '{slug}' is missing field '{field_path}' "
                        f"(needed for header {header_key} on {server_name})"
                    )
                resolved[key] = str(value)

            def _sub(m: "re.Match[str]") -> str:
                return resolved[f"{m.group(1)}:{m.group(2)}"]

            headers[header_key] = _PLACEHOLDER_RE.sub(_sub, template)

        return ResolvedCredentials(
            env_vars=env_vars,
            headers=headers,
            used_credential_ids=[cid for cid, _ in cred_cache.values()],
        )

    @staticmethod
    async def validate(server: MCPServer, user) -> list[str]:
        """
        Dry-run credential resolution. Returns a list of human-readable error
        strings (empty = OK). Used by pre-execution workflow validation.
        """
        try:
            await CredentialInjector.resolve(server, user)
            return []
        except CredentialMissingError as e:
            return [str(e)]
        except CredentialInvalidError as e:
            return [str(e)]
        except Exception as e:  # noqa: BLE001
            logger.exception("Unexpected error validating credentials for %s", server.name)
            return [f"Unexpected error validating '{server.name}': {type(e).__name__}"]
