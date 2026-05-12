"""
Exception types raised by the executor subsystem.

Keep these in one module so consumers (views, tasks, tests) can import a
single catch-block without circular-import headaches through king.py.
"""
from __future__ import annotations


class AuthorizationError(Exception):
    """Raised when a user is not authorized to access an execution."""


class HITLTimeoutError(Exception):
    """Raised when a human-in-the-loop request times out."""


class StateConflictError(Exception):
    """Raised when an operation is invalid for the current execution state."""


class ExecutionNotFoundError(Exception):
    """Raised when an execution cannot be located."""


class LLMProviderError(Exception):
    """
    Raised when the orchestrator's LLM call fails.

    Has two subtypes callers often branch on:
      * connection failure — the provider is unreachable
      * content failure    — the provider responded but the response is unusable

    Check `is_connection_error` rather than string-matching on the message.
    """
    def __init__(self, message: str, *, is_connection_error: bool = False):
        super().__init__(message)
        self.is_connection_error = is_connection_error
