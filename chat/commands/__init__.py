"""Slash commands in the chat box (P10, §18).

A command is structured input, not a prompt template. The client sends
`TurnRequest.command = {"name": ..., "args": {...}, "text": ...}` and the
backend resolves it. The registry is code, declared the way tools are
(registration *is* the schema).
"""
from .registry import (
    Arg,
    Command,
    CommandCall,
    CommandContext,
    CommandResult,
    ResolvedCommand,
    command,
    get,
    listing,
)

__all__ = [
    "Arg",
    "Command",
    "CommandCall",
    "CommandContext",
    "CommandResult",
    "ResolvedCommand",
    "command",
    "get",
    "listing",
]
