"""
Outbound network safety.

Anything in this codebase that fetches a URL chosen by a user, an agent, or a
workflow config goes through here. That is a wide surface — the HTTP connector,
the chat scraping tools, every REST connector — and each of them was previously
free to make its own decision about what is safe to call.

The threat is server-side request forgery. This process runs on EC2 inside a
VPC, so "make an HTTP request to a URL I give you" is, without a guard, also
"read the instance metadata service" and "reach anything on the private network
that has no authentication because it assumed it was unreachable".

Known limitation, stated rather than papered over: validate() resolves the
hostname and then hands the URL to httpx, which resolves it again. A DNS record
that changes between those two lookups (rebinding) defeats the check. Closing
that properly means pinning the resolved IP into the connection, which httpx
does not expose cleanly. The window is small and the check still stops the
overwhelming majority of real cases — direct IPs, localhost, metadata hostnames
— so it is worth having, but it is not a guarantee.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

# Ranges that must never be reachable from a user-supplied URL.
_BLOCKED_IP_RANGES = [
    ipaddress.ip_network('0.0.0.0/8'),
    ipaddress.ip_network('10.0.0.0/8'),
    ipaddress.ip_network('127.0.0.0/8'),
    ipaddress.ip_network('169.254.0.0/16'),  # AWS/GCP/Azure instance metadata
    ipaddress.ip_network('172.16.0.0/12'),
    ipaddress.ip_network('192.168.0.0/16'),
    ipaddress.ip_network('100.64.0.0/10'),   # CGNAT, used by some cloud fabrics
    ipaddress.ip_network('::1/128'),
    ipaddress.ip_network('fc00::/7'),
    ipaddress.ip_network('fe80::/10'),
]

_BLOCKED_HOSTNAMES = {
    'metadata.google.internal',
    'metadata.google',
    'metadata',
    'kubernetes.default',
    'kubernetes.default.svc',
    'kubernetes.default.svc.cluster.local',
}

# Response headers that must not be handed back into workflow data. A workflow
# stores its node output in the DB and shows it in the UI, so echoing these
# copies someone's session cookie or bearer token into both.
_SENSITIVE_RESPONSE_HEADERS = {
    'set-cookie',
    'authorization',
    'proxy-authorization',
    'www-authenticate',
    'proxy-authenticate',
    'x-api-key',
    'x-auth-token',
    'x-amz-security-token',
}


class UnsafeURLError(ValueError):
    """Raised when a URL points somewhere a user-supplied URL must not reach."""


def validate_url(url: str) -> tuple[bool, str]:
    """
    Check whether a URL is safe to fetch. Returns (is_safe, reason_if_not).

    Blocking rules, in order: scheme must be http(s); hostname must be present
    and not a known metadata alias; every address the hostname resolves to must
    be outside the private/link-local ranges. All resolved addresses are checked,
    not just the first — a hostname with both a public and a private A record
    would otherwise pass on a lucky ordering.
    """
    try:
        parsed = urlparse(url)
    except Exception:
        return False, "Invalid URL format"

    if parsed.scheme not in ('http', 'https'):
        return False, f"URL scheme '{parsed.scheme}' is not allowed. Only http and https are permitted."

    hostname = parsed.hostname
    if not hostname:
        return False, "URL has no hostname"

    if hostname.lower().rstrip('.') in _BLOCKED_HOSTNAMES:
        return False, f"Access to '{hostname}' is blocked"

    # A literal IP needs no DNS round trip, and going through getaddrinfo for one
    # would mean a needless lookup on every request.
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None

    if literal is not None:
        for blocked in _BLOCKED_IP_RANGES:
            if literal in blocked:
                return False, "Access to internal or private network addresses is blocked"
        return True, ""

    try:
        resolved = socket.getaddrinfo(
            hostname, parsed.port or (443 if parsed.scheme == 'https' else 80),
            proto=socket.IPPROTO_TCP,
        )
    except socket.gaierror:
        return False, f"Could not resolve hostname '{hostname}'"

    for *_unused, sockaddr in resolved:
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except ValueError:
            continue
        for blocked in _BLOCKED_IP_RANGES:
            if ip in blocked:
                return False, "Access to internal or private network addresses is blocked"

    return True, ""


async def validate_url_async(url: str) -> tuple[bool, str]:
    """
    validate_url for async callers.

    getaddrinfo is a blocking syscall. Called straight from a coroutine it stalls
    the event loop for the duration of the DNS lookup — which, for a hostname
    that does not resolve, is however long the resolver takes to give up. Every
    other request on that worker waits. Hence the thread.
    """
    return await asyncio.to_thread(validate_url, url)


def assert_url_safe(url: str) -> None:
    """validate_url, but raises. For call sites that would only ever raise anyway."""
    ok, reason = validate_url(url)
    if not ok:
        raise UnsafeURLError(reason)


def redact_headers(headers) -> dict[str, str]:
    """
    Copy response headers, replacing sensitive values with a marker.

    The names are kept: knowing that a Set-Cookie came back is often exactly what
    someone is debugging, and dropping the key entirely makes it look as though
    the server never sent one.
    """
    out: dict[str, str] = {}
    for key, value in dict(headers).items():
        out[key] = '[redacted]' if key.lower() in _SENSITIVE_RESPONSE_HEADERS else value
    return out
