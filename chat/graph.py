"""
LangGraph Chat Agent — Standardized Agentic Tool Loop

Replaces the manual for-loop in views.py with a LangGraph StateGraph.
The graph handles: agent → tools → agent cycling with automatic
recursion limits and structured message threading.
"""
import asyncio
import json
import logging
from typing import Any, TypedDict, Annotated, Optional

from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, ToolMessage,
)
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages

logger = logging.getLogger(__name__)

# Timeouts
LLM_CALL_TIMEOUT = 180
TOOL_CALL_TIMEOUT = 120
MAX_THINKING_CHUNKS = 100000

# Max tokens by intent — complex tasks need more output room to finish JSON
INTENT_MAX_TOKENS = {
    "coding": 16384,
    "research": 16384,
    "file_manipulation": 16384,
}
DEFAULT_MAX_TOKENS = 8192


# ─────────────── State Schema ───────────────

class ChatAgentState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    metadata: dict
    tool_trace: list
    thinking: str
    total_tokens: int
    # Read-only context
    provider: str
    model: str
    system_message: str
    user_id: int
    response_format: str
    clean_content: str
    intent: str
    history_list: list
    attachments: list
    max_iterations: int
    stream_callback: Optional[Any]


# ─────────────── Helpers ───────────────

def _openai_tc_to_langchain(raw_tool_calls: list) -> list[dict]:
    """Convert OpenAI-format tool_calls to LangChain AIMessage format."""
    from chat.extraction import parse_tool_arguments
    converted = []
    for i, tc in enumerate(raw_tool_calls):
        if tc.get("type") != "function":
            continue
        func = tc.get("function", {})
        name = func.get("name", "")
        if not name:
            continue
        args = parse_tool_arguments(func.get("arguments", {}))
        converted.append({
            "name": name,
            "args": args if isinstance(args, dict) else {"query": str(args)},
            "id": tc.get("id") or f"call_{i}",
        })
    return converted


def _count_ai_messages(messages: list) -> int:
    return sum(1 for m in messages if isinstance(m, AIMessage))


# ─────────────── Agent Node ───────────────

async def agent_node(state: ChatAgentState) -> dict:
    """Call the LLM. Returns AIMessage (with or without tool_calls)."""
    from chat.views import execute_llm
    from chat.extraction import extract_tool_calls
    import chat.tools as shared_tools

    callback = state.get("stream_callback")
    provider = state["provider"]
    model = state["model"]
    iteration = _count_ai_messages(state["messages"])
    at_limit = iteration >= state.get("max_iterations", 30) - 1

    # ── Build prompt ──
    # Find original human message
    original_prompt = ""
    for msg in state["messages"]:
        if isinstance(msg, HumanMessage):
            original_prompt = msg.content
            break

    trajectory = []
    for msg in state["messages"][1:]:
        if isinstance(msg, AIMessage):
            calls = []
            if msg.tool_calls:
                calls = [f"{tc['name']}({tc.get('args', {})})" for tc in msg.tool_calls]
            
            step = "Assistant Action:"
            if msg.content:
                step += f"\n{msg.content}"
            if calls:
                step += f"\nCalls: {', '.join(calls)}"
            if step != "Assistant Action:":
                trajectory.append(step)
        elif isinstance(msg, ToolMessage):
            trajectory.append(f"Tool '{msg.name}' Result:\n{msg.content}")

    if trajectory:
        combined_traj = "\n\n".join(trajectory)
        prompt = (
            f"{original_prompt}\n\n"
            f"--- PREVIOUS ACTIONS & TOOL RESULTS IN THIS TURN ---\n"
            f"You have already taken the following steps. Review carefully. "
            f"DO NOT repeat the exact same tool calls.\n\n"
            f"{combined_traj}\n\n"
            f"--- END PREVIOUS ACTIONS ---\n\n"
        )
        if at_limit:
            prompt += (
                "You have reached the tool-iteration limit. You MUST now provide "
                "your final answer in the required JSON format. Do NOT call tools again."
            )
        else:
            prompt += (
                "If you need more information, call additional tools. "
                "Otherwise, provide your final answer in the requested format."
            )
    else:
        prompt = original_prompt

    # Don't pass tools if at iteration limit (forces final answer)
    tools_payload = None if at_limit else shared_tools.AVAILABLE_TOOLS

    thinking_delta = ""
    content = ""
    tool_calls_raw = []
    tokens_used = 0

    if callback:
        await callback("status", {"phase": "thinking", "message": f"Analyzing context (Round {iteration + 1})..."})

    # ── Resolve max_tokens for this intent ──
    intent = state.get("intent", "chat")
    resolved_max_tokens = INTENT_MAX_TOKENS.get(intent, DEFAULT_MAX_TOKENS)

    # ── Call LLM ──
    if callback:
        # Streaming mode
        try:
            stream_gen = await execute_llm(
                provider=provider, model=model, prompt=prompt,
                system_message=state["system_message"],
                user_id=state["user_id"],
                max_tokens=resolved_max_tokens,
                tools=tools_payload,
                history=state.get("history_list", []),
                response_format=state["response_format"],
                attachments=state.get("attachments", []),
                stream=True,
            )
            async with asyncio.timeout(LLM_CALL_TIMEOUT):
                chunk_count = 0
                async for chunk in stream_gen:
                    chunk_count += 1
                    if chunk_count > MAX_THINKING_CHUNKS:
                        break
                    if chunk["type"] == "content":
                        content += chunk["content"]
                    elif chunk["type"] == "thinking":
                        thinking_delta += chunk["content"]
                        await callback("thinking_chunk", {"content": chunk["content"]})
                    elif chunk["type"] == "tool_calls":
                        for tc in chunk["tool_calls"]:
                            idx = tc.get("index", 0)
                            while len(tool_calls_raw) <= idx:
                                tool_calls_raw.append({"type": "function", "function": {"name": "", "arguments": ""}})
                            target = tool_calls_raw[idx]
                            if "id" in tc:
                                target["id"] = tc["id"]
                            if "function" in tc:
                                if "name" in tc["function"]:
                                    target["function"]["name"] += tc["function"]["name"]
                                if "arguments" in tc["function"]:
                                    target["function"]["arguments"] += tc["function"]["arguments"]
                    elif chunk["type"] == "metadata":
                        usage = chunk.get("usage", {})
                        tokens_used += usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                    elif chunk["type"] == "error":
                        logger.error(f"[Agent Node] LLM error: {chunk.get('message')}")
                        content = f"LLM Error: {chunk.get('message', 'Unknown')}"
                        break
        except asyncio.TimeoutError:
            logger.warning(f"[Agent Node] LLM stream timed out")
            if not content:
                content = "Response timed out. Please try again."
    else:
        # Non-streaming mode
        try:
            result = await asyncio.wait_for(
                execute_llm(
                    provider=provider, model=model, prompt=prompt,
                    system_message=state["system_message"],
                    user_id=state["user_id"],
                    max_tokens=resolved_max_tokens,
                    tools=tools_payload,
                    history=state.get("history_list", []),
                    response_format=state["response_format"],
                    attachments=state.get("attachments", []),
                    stream=False,
                ),
                timeout=LLM_CALL_TIMEOUT,
            )
            content = result.get("content", "")
            tool_calls_raw = result.get("tool_calls", [])
            tokens_used = result.get("usage", {}).get("total_tokens", 0)
            if result.get("thinking"):
                thinking_delta = result["thinking"]
        except asyncio.TimeoutError:
            content = "Response timed out. Please try again."
        except Exception as e:
            logger.exception(f"[Agent Node] LLM call failed: {e}")
            content = f"Internal Error: {str(e)}"

    # ── Fallback: extract hallucinated tool calls from text ──
    if not at_limit and not tool_calls_raw and content:
        extracted = extract_tool_calls(content)
        if extracted:
            for tc in extracted:
                content = content.replace(tc["raw"], "")
                tool_calls_raw.append({
                    "type": "function",
                    "function": {"name": tc["tool"], "arguments": tc["args"]},
                })
            content = content.strip()

    lc_tool_calls = _openai_tc_to_langchain(tool_calls_raw)

    ai_message = AIMessage(
        content=content or "",
        tool_calls=lc_tool_calls if lc_tool_calls else [],
    )

    return {
        "messages": [ai_message],
        "thinking": state.get("thinking", "") + (f"{thinking_delta}\n\n" if thinking_delta else ""),
        "total_tokens": state.get("total_tokens", 0) + tokens_used,
    }


# ─────────────── Tools Node ───────────────

async def tools_node(state: ChatAgentState) -> dict:
    """Execute tool calls from the last AIMessage."""
    import chat.tools as shared_tools
    from chat.views import perform_image_search, _sanitize_tool_args

    last_msg = state["messages"][-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"messages": []}

    callback = state.get("stream_callback")
    metadata = dict(state.get("metadata", {}))
    tool_trace = list(state.get("tool_trace", []))
    clean_content = state.get("clean_content", "")
    thinking = state.get("thinking", "")
    iteration = _count_ai_messages(state["messages"])

    tool_messages = []

    for tc in last_msg.tool_calls:
        fn = tc["name"]
        args = dict(tc.get("args", {}))
        call_id = tc.get("id", f"call_{fn}")

        args = _sanitize_tool_args(args)
        if fn == "web_search" and not args.get("query"):
            args["query"] = clean_content
        if fn == "suggest_workflow" and not args.get("intent"):
            args["intent"] = clean_content
        # Guard: if code tool was called with empty code, give the LLM a helpful retry hint
        if fn == "execute_python_code" and not args.get("code"):
            logger.warning(f"[Tools Node] execute_python_code called with empty code. Raw args: {tc.get('args', {})}")
            tool_messages.append(ToolMessage(
                content="Error: The 'code' argument was empty. You must provide Python code in the 'code' parameter. Example: {\"code\": \"print(2+2)\"}",
                tool_call_id=call_id,
                name=fn,
            ))
            continue

        # Trace
        thought_ctx = thinking.strip()[-150:] if thinking else ""
        if fn == "execute_python_code" and args.get("code"):
            thought_ctx = f"**Executing Code:**\n```python\n{args['code']}\n```\n\n{thought_ctx}"
        trace_entry = {"tool": fn, "args": args, "iteration": iteration, "thought": thought_ctx, "summary": thought_ctx}
        tool_trace.append(trace_entry)

        if callback:
            await callback("agent_trace", {"sub_type": "tool", "tool": fn, "args": args, "iteration": iteration, "thought": thought_ctx, "summary": thought_ctx})

        logger.info(f"[Tools Node iter={iteration}] Calling: {fn} | args keys: {list(args.keys())} | code_present: {'code' in args and bool(args.get('code'))}")

        # Execute
        ctx = {"user_id": state["user_id"]}
        try:
            res = await asyncio.wait_for(shared_tools.execute_tool(fn, args, ctx), timeout=TOOL_CALL_TIMEOUT)
        except asyncio.TimeoutError:
            res = f"Error: Tool {fn} timed out after {TOOL_CALL_TIMEOUT}s"
        except Exception as e:
            res = f"Error executing {fn}: {str(e)}"

        # ── Side-effects: update metadata ──
        try:
            if fn == "web_search":
                metadata["search_query"] = args.get("query", clean_content)
                parsed = json.loads(res)
                if parsed.get("type") == "search_results":
                    existing = metadata.get("sources", [])
                    seen = {s.get("url") for s in existing}
                    for src in parsed.get("sources", []):
                        if src.get("url") not in seen:
                            existing.append(src)
                            seen.add(src.get("url"))
                    metadata["sources"] = existing[:50]
                    if callback:
                        await callback("sources_update", {"sources": existing[:50]})
                    # Proactive image search
                    try:
                        img_res = await perform_image_search(args.get("query", clean_content))
                        if img_res.get("images"):
                            imgs = metadata.get("images", [])
                            imgs.extend(img_res["images"])
                            metadata["images"] = imgs
                            if callback:
                                await callback("images_update", {"images": imgs})
                    except Exception:
                        pass
            elif fn == "image_search":
                parsed = json.loads(res)
                if parsed.get("type") == "image_results" and parsed.get("images"):
                    imgs = metadata.get("images", [])
                    imgs.extend(parsed["images"])
                    metadata["images"] = imgs
                    if callback:
                        await callback("images_update", {"images": imgs})
            elif fn == "video_search":
                parsed = json.loads(res)
                if parsed.get("type") == "video_results" and parsed.get("videos"):
                    vids = metadata.get("videos", [])
                    vids.extend(parsed["videos"])
                    metadata["videos"] = vids
                    if callback:
                        await callback("videos_update", {"videos": vids})
            elif fn == "suggest_workflow":
                parsed = json.loads(res)
                if parsed.get("found"):
                    metadata["workflow_id"] = parsed.get("workflow_id")
                    metadata["workflow_name"] = parsed.get("name")
            elif fn == "execute_python_code":
                parsed = json.loads(res)
                execs = metadata.get("code_executions", [])
                execs.append({"code": args.get("code", ""), "output": parsed.get("output", ""), "result": parsed.get("result", ""), "iteration": iteration})
                metadata["code_executions"] = execs
                metadata["has_code_execution"] = True
                if callback:
                    await callback("code_execution", {"code": args.get("code", ""), "output": parsed.get("output", ""), "result": parsed.get("result", ""), "error": parsed.get("error", "") if parsed.get("status") == "error" else ""})
        except (json.JSONDecodeError, TypeError, AttributeError):
            pass

        tool_messages.append(ToolMessage(content=str(res), tool_call_id=call_id, name=fn))

    return {
        "messages": tool_messages,
        "metadata": metadata,
        "tool_trace": tool_trace,
    }


# ─────────────── Edge Logic ───────────────

def should_continue(state: ChatAgentState) -> str:
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        return "tools"
    return END


# ─────────────── Build Graph ───────────────

def build_chat_agent_graph():
    graph = StateGraph(ChatAgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", tools_node)
    graph.set_entry_point("agent")
    graph.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
    graph.add_edge("tools", "agent")
    return graph.compile()


chat_agent_graph = build_chat_agent_graph()


# ─────────────── Public API ───────────────

async def run_agent_loop(
    *,
    full_prompt: str,
    metadata: dict,
    provider: str,
    model: str,
    system_message: str,
    user_id: int,
    response_format: str,
    clean_content: str,
    intent: str,
    history_list: list,
    attachments: list | None = None,
    stream_callback=None,
    max_iterations: int = 30,
) -> dict:
    """
    Run the agentic tool loop via LangGraph.

    Returns dict with keys:
        raw_content, metadata, tool_trace, thinking, total_tokens,
        interrupted, accumulated_tool_context
    """
    from chat.views import resolve_agent_iteration_limit

    if max_iterations <= 0:
        max_iterations = resolve_agent_iteration_limit(intent)

    initial_state: ChatAgentState = {
        "messages": [HumanMessage(content=full_prompt)],
        "metadata": dict(metadata),
        "tool_trace": [],
        "thinking": "",
        "total_tokens": 0,
        "provider": provider,
        "model": model,
        "system_message": system_message,
        "user_id": user_id,
        "response_format": response_format,
        "clean_content": clean_content,
        "intent": intent,
        "history_list": history_list,
        "attachments": attachments or [],
        "max_iterations": max_iterations,
        "stream_callback": stream_callback,
    }

    interrupted = False
    try:
        result = await chat_agent_graph.ainvoke(
            initial_state,
            config={"recursion_limit": max_iterations * 2 + 10},
        )
    except Exception as e:
        logger.exception(f"[LangGraph] Graph execution failed: {e}")
        # Return whatever we have
        return {
            "raw_content": f"Error: {str(e)}",
            "metadata": metadata,
            "tool_trace": [],
            "thinking": "",
            "total_tokens": 0,
            "interrupted": True,
            "accumulated_tool_context": [],
        }

    # Extract final content from last AIMessage
    raw_content = ""
    for msg in reversed(result["messages"]):
        if isinstance(msg, AIMessage):
            raw_content = msg.content
            break

    # Build accumulated_tool_context from ToolMessages (for post-processing compat)
    acc = []
    for msg in result["messages"]:
        if isinstance(msg, ToolMessage):
            acc.append(f"[Tool: {msg.name}]\nResult: {msg.content}")

    return {
        "raw_content": raw_content,
        "metadata": result.get("metadata", metadata),
        "tool_trace": result.get("tool_trace", []),
        "thinking": result.get("thinking", ""),
        "total_tokens": result.get("total_tokens", 0),
        "interrupted": interrupted,
        "accumulated_tool_context": acc,
    }
