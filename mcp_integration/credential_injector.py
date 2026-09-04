"""
CredentialInjector — the single point of truth for resolving MCP server
credentials from the `credentials` app and materialising them into the shape
an MCP server process (stdio) or HTTP client (SSE) expects.

All MCP code paths — client manager, workflow validator, tool provider — go
through this module. Do not re-implement credential resolution elsewhere.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

from asgiref.sync import sync_to_async

from .models import MCPServer

logger = logging.getLogger(__name__)


# Matches `{slug:field}` placeholders inside header values.
_PLACEHOLDER_RE = re.compile(r"\{(@?[a-zA-Z0-9_\-]+):([a-zA-Z0-9_\-.]+)\}")

# Matches `slug:field` used as a whole value (env var map format).
_MAPPING_RE = re.compile(r"^(@?[a-zA-Z0-9_\-]+):([a-zA-Z0-9_\-.]+)$")

# Sentinel slug meaning "read this from Django settings, not from the user's
# vault". Needed for values that belong to the *platform* rather than the user:
# a Google OAuth client id/secret is ours, and asking every user to create their
# own GCP project just to read their calendar is the single biggest reason a
# curated connector goes unused.
SETTINGS_SLUG = "@settings"


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


@dataclass(frozen=True)
class CredentialFile:
    """
    One credential file a server wants on disk, already rendered.

    Deliberately *not* written here. `resolve()` is pure — it is called by
    `validate()` on every Connections page load, and a dry run that scattered
    plaintext refresh tokens across the filesystem would be a strange thing for
    a diagnostic to do. The bytes are handed to `_SessionWorker`, which creates
    the directory and removes it in the same task it opened the session in.
    """
    env_var: str
    filename: str
    content: str
    #: "file" — the env var receives the file's own path.
    #: "dir"  — it receives the containing directory, for servers that expect a
    #: credentials *folder* and pick the filename themselves.
    target: str = "file"


@dataclass
class ResolvedCredentials:
    """Materialised credential values keyed by the server's mapping target."""
    env_vars: dict[str, str]
    headers: dict[str, str]
    used_credential_ids: list[int]
    files: list[CredentialFile] = field(default_factory=list)


async def _resolve_slug(
    user_id: int, slug: str, server_name: str
) -> tuple[int, dict[str, Any]]:
    """
    The user's credential of this type, as `(id, decrypted fields)`.

    Delegates to `credentials.manager` rather than querying here. This module
    used to carry its own lookup and its own decrypt, and the cost of that
    second implementation was everything the first one had: no 5-minute cache,
    so every connector re-queried and re-decrypted on every turn, and no OAuth
    refresh. The lookup's ordering (verified first, most recently updated
    second) moved with it and is documented there.

    The row is resolved separately from its data because the id is worth
    keeping: `used_credential_ids` is what an audit entry would hang off, and a
    dict of fields cannot say which row it came from.
    """
    from credentials.manager import get_credential_manager

    manager = get_credential_manager()
    cred = await sync_to_async(manager.lookup_by_slug_sync)(slug, user_id)
    if cred is None:
        raise CredentialMissingError(slug, server_name)
    data = await manager.get_credential(cred.id, user_id)
    if data is None:
        # The row exists but could not be decrypted at all — a different
        # failure from "you have not connected this", and a different fix.
        raise CredentialInvalidError(
            f"Credential '{slug}' could not be read (id={cred.id}). "
            f"Try reconnecting it."
        )
    return cred.id, data


def _extract_field(data: dict[str, Any], field_path: str) -> Any:
    cur: Any = data
    for part in field_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _settings_value(name: str) -> Any:
    """Read a platform-owned value out of Django settings."""
    from django.conf import settings as django_settings

    return getattr(django_settings, name, None) or None


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
                cred_cache[slug] = await _resolve_slug(user_id, slug, server_name)
            return cred_cache[slug]

        # An OAuth authorization, when the user has completed one, satisfies a
        # remote server on its own: it *is* the credential, and requiring a
        # separately-pasted token alongside it would be asking twice for the
        # same access. Checked before `required_credential_types` so a curated
        # row can declare a token type as its manual fallback without that
        # declaration blocking the users who connected instead.
        # `getattr`, not attribute access: `resolve` is called with lightweight
        # stand-ins for a server (tests use SimpleNamespace, and an unsaved row
        # has no pk). Neither can have an authorization, so "no id" and "not
        # remote" are the same answer — no bearer — rather than an AttributeError.
        from .oauth import access_token_for

        oauth_bearer = None
        server_id = getattr(server, "id", None)
        if server_id and getattr(server, "type", None) in MCPServer.REMOTE_TYPES:
            oauth_bearer = await sync_to_async(access_token_for)(server_id, user_id)

        # Required types must exist regardless of whether they're mapped.
        if not oauth_bearer:
            for slug in server.required_credential_types or []:
                if slug == SETTINGS_SLUG:
                    continue
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
            if slug == SETTINGS_SLUG:
                value = _settings_value(field_path)
                if value is None:
                    raise CredentialInvalidError(
                        f"Platform setting '{field_path}' is not configured "
                        f"(needed for env var {env_key} on {server_name})"
                    )
                env_vars[env_key] = str(value)
                continue
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
                if slug == SETTINGS_SLUG:
                    setting = _settings_value(field_path)
                    if setting is None:
                        raise CredentialInvalidError(
                            f"Platform setting '{field_path}' is not configured "
                            f"(needed for header {header_key} on {server_name})"
                        )
                    resolved[key] = str(setting)
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

        async def _fill(text: str) -> str:
            """Substitute every {slug:field} in one string. Same grammar as a
            header template, so a connector author learns one syntax."""
            out = text
            for p in list(_PLACEHOLDER_RE.finditer(text)):
                slug, field_path = p.group(1), p.group(2)
                if slug == SETTINGS_SLUG:
                    value = _settings_value(field_path)
                    if value is None:
                        raise CredentialInvalidError(
                            f"Platform setting '{field_path}' is not configured "
                            f"(needed by a credential file on {server_name})"
                        )
                else:
                    _, data = await _get(slug)
                    value = _extract_field(data, field_path)
                    if value is None:
                        raise CredentialInvalidError(
                            f"Credential '{slug}' is missing field '{field_path}' "
                            f"(needed by a credential file on {server_name})"
                        )
                out = out.replace(p.group(0), str(value))
            return out

        async def _render(node):
            """Walk the content tree, substituting inside string leaves only.
            Keys are left alone: a placeholder in a key would produce a JSON
            document whose shape depends on a secret, which no server expects."""
            if isinstance(node, str):
                return await _fill(node)
            if isinstance(node, dict):
                return {k: await _render(v) for k, v in node.items()}
            if isinstance(node, list):
                return [await _render(v) for v in node]
            return node

        files: list[CredentialFile] = []
        for env_key, spec in (server.credential_file_map or {}).items():
            if not isinstance(spec, dict):
                logger.warning(
                    "Skipping non-object file spec %r on server %s", spec, server_name
                )
                continue
            filename = spec.get("filename")
            if not filename or "/" in filename or "\\" in filename or filename.startswith("."*2):
                # The filename lands inside a directory we create; a separator
                # or a parent reference would let a catalogue row write outside
                # it. Curated rows are ours, but this map is also editable on a
                # user's own server.
                raise CredentialInvalidError(
                    f"Credential file for {env_key} on {server_name} has an "
                    f"unusable filename {filename!r}"
                )
            content = spec.get("content")
            if content is None:
                raise CredentialInvalidError(
                    f"Credential file for {env_key} on {server_name} has no content"
                )
            rendered = await _render(content)
            files.append(CredentialFile(
                env_var=env_key,
                filename=filename,
                content=rendered if isinstance(rendered, str) else json.dumps(rendered),
                target=spec.get("target", "file"),
            ))

        if oauth_bearer:
            # `setdefault`, not assignment: an explicit `credential_header_map`
            # entry is configuration someone wrote deliberately, and silently
            # overwriting it would make a hand-set Authorization header
            # impossible to use on a server the user had also connected.
            headers.setdefault("Authorization", f"Bearer {oauth_bearer}")

        return ResolvedCredentials(
            env_vars=env_vars,
            headers=headers,
            used_credential_ids=[cid for cid, _ in cred_cache.values()],
            files=files,
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
