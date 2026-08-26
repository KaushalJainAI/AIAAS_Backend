"""
Shared tool registry for agentic execution.

Importing this package is what populates the registry: each submodule declares
its own tools with `@tool(...)`, so a tool's schema, its implementation and its
availability rule are one block of code rather than three lists that have to be
kept in step by hand.

What lives where:

  web           the open web — search, research, fetch, scrape
  knowledge     the user's knowledge bases, through the RAG tiers
  conversation  reading back from this session's own record
  agents        finding and running the user's saved agents
  sandbox       the wasm Python sandbox
  artifacts     the sandboxed-iframe HTML renderer
  vision        the `ask_vision` surface over `chat.vision`
  internal      this platform's own API, called as the user
  clock         wall-clock time

MCP tools are not registered here. They are minted at runtime from a
third-party catalogue, so they are resolved on every call and every listing
instead — see `mcp_integration.tool_provider`.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List

from . import (  # noqa: F401  — imported for their registration side effect
    agents,
    artifacts,
    clock,
    conversation,
    internal,
    knowledge,
    sandbox,
    vision,
    web,
)
from .registry import Tool, all_tools, get, schemas, sensitive_names, tool

logger = logging.getLogger(__name__)

__all__ = [
    "AVAILABLE_TOOLS",
    "SENSITIVE_TOOLS",
    "Tool",
    "all_tools",
    "execute_tool",
    "get",
    "get_available_tools",
    "schemas",
    "tool",
]

#: The full built-in catalogue, ungated. Callers that need one user's view want
#: `get_available_tools`; this is for code that filters by its own rules, like
#: an agent's allow-list.
AVAILABLE_TOOLS: List[Dict[str, Any]] = schemas()

#: Tools that require Human-In-The-Loop approval before execution, derived from
#: the tools' own `sensitive=True`. It is a short list on purpose — see
#: `chat.permissions`, which gates by policy rather than by name, and covers the
#: MCP tools that could never appear in a list of names minted at build time.
#:
#: `execute_python` is deliberately absent, unlike in `agent_runtime`, where an
#: unattended agent asks before running code. Here a human wrote the message
#: that produced the call and is watching the answer arrive, and the sandbox has
#: no network, no filesystem and no imports that reach either — so the prompt
#: would be asking permission for arithmetic. `run_agent` *is* sensitive for the
#: mirror reason: it hands work to something the user is not watching.
#:
#: `render_html_artifact` is deliberately absent too. It renders inside a
#: sandboxed iframe with no network, no same-origin access and no session, so
#: there is nothing for a human to meaningfully approve — and prompting on every
#: chart would train users to click through approvals without reading them,
#: which is what makes the prompts on the remaining tools worthless.
SENSITIVE_TOOLS: List[str] = sensitive_names()


async def _requirement_met(
    requirement: str,
    user_id: int | None,
    memory_enabled: bool,
    session_key: str | None,
) -> bool:
    """
    Whether a tool's precondition holds for this caller.

    Unmet means the tool is not offered at all. An advertised tool that cannot
    run is worse than one never offered, because the model plans around it and
    then has to explain the failure.
    """
    if requirement == "memory":
        return memory_enabled
    if requirement == "spill":
        return await conversation.has_spilled_output(user_id, session_key)
    if requirement == "vision":
        if user_id is None:
            return False  # no user, no credential, no witness
        try:
            from ..vision import witness_available

            return await witness_available(user_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not resolve vision witness for user {user_id}: {e}")
            return False
    return True


async def get_available_tools(
    user_id: int | None,
    memory_enabled: bool = True,
    session_key: str | None = None,
) -> List[Dict[str, Any]]:
    """
    Return the full tool list for this user: built-in tools whose requirements
    are met, plus any MCP tools the user has enabled. Safe to call on every
    agent turn (MCP tool lists are cached in Redis).
    """
    tools: List[Dict[str, Any]] = []
    for entry in all_tools():
        if entry.requires and not await _requirement_met(
            entry.requires, user_id, memory_enabled, session_key
        ):
            continue
        tools.append(entry.schema)

    if user_id is None:
        return tools

    try:
        from mcp_integration.tool_provider import MCPToolProvider

        tools.extend(await MCPToolProvider.get_openai_tool_descriptors(user_id))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"Could not load MCP tools for user {user_id}: {e}")
    return tools


async def execute_tool(
    func_name: str, args: Dict[str, Any], context: Dict[str, Any]
) -> str:
    """Execute a tool by name and return its string response."""
    # Deliberately two try blocks. Wrapping the *import* and the dispatch
    # together meant any failure inside mcp_integration — an import error, a
    # misconfigured app — returned "Error executing MCP tool web_search" for
    # every built-in and never reached the registry below. A broken MCP
    # subsystem must cost the user their MCP tools, not all of them.
    try:
        from mcp_integration.tool_provider import MCPToolProvider, is_mcp_tool

        is_mcp = is_mcp_tool(func_name)
    except Exception as e:  # noqa: BLE001
        logger.error(f"MCP tool provider unavailable for {func_name}: {e}")
        is_mcp = False

    if is_mcp:
        try:
            return await MCPToolProvider.execute(func_name, args, context.get("user_id"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP dispatch failed for {func_name}: {e}")
            return f"Error executing MCP tool {func_name}: {str(e)}"

    entry = get(func_name)
    if entry is None:
        return f"Error: Tool '{func_name}' is not recognized."

    try:
        return await entry.run(args, context)
    except Exception as e:  # noqa: BLE001
        logger.error(f"Error executing tool {func_name}: {e}")
        return f"Error executing tool {func_name}: {str(e)}"
