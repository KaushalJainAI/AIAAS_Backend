"""
Who the caller is and what they may do.

`authentication` resolves an identity (JWT, API key); `permissions` decides what
that identity may reach. Kept apart from `core.safety`, which is about
untrusted *content* rather than untrusted callers.
"""
