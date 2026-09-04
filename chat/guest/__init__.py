"""
The unauthenticated guest surface.

Deliberately its own package: guests get one owner, one provider, no uploads, no
KB, no MCP, and IP-based rate limits. Keeping it apart is what stops a change to
the authenticated pipeline from silently widening what a visitor can reach.
"""
