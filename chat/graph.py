"""
LangGraph Chat Agent — Standardized Agentic Tool Loop

Replaces the manual for-loop in views.py with a LangGraph StateGraph.
The graph handles: agent → tools → agent cycling with automatic
recursion limits and structured message threading.
"""
import asyncio
import json
import logging
from typing import TypedDict, Annotated

from langchain_core.messages import (
    BaseMessage, HumanMessage, AIMessage, ToolMessage,
)
from langgraph.graph import StateGraph, END
from langgraph.graph.message import add_messages
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import interrupt
from langchain_core.runnables import RunnableConfig

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
    thread_id: str
    response_format: str
    clean_content: str
    intent: str
    history_list: list
    max_iterations: int
    memory_enabled: bool


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


async def _is_vision_enabled_model(model_value: str, provider_slug: str) -> bool:
    """Check the database to see if this model supports image input (vision)."""
    try:
        from nodes.models import AIModel
        
        # Look up by model technical value (e.g. 'gpt-4o', 'gemini-1.5-pro')
        model_obj = await AIModel.objects.filter(value=model_value, provider__slug=provider_slug).afirst()
        if model_obj:
            return model_obj.supports_image_input
            
        # Fallback to keyword matching for models not in the registry (e.g. new OpenRouter models)
        m = model_value.lower()
        vision_keywords = [
            "vision", "gpt-4o", "gpt-4-turbo", "gemini", "claude-3", 
            "pixtral", "llama-3.2", "qwen-vl", "lava", "moondream", "llava"
        ]
        if any(k in m for k in vision_keywords) and "gpt-3.5" not in m:
            return True
    except Exception:
        pass
    return False


# ─────────────── Agent Node ───────────────

async def agent_node(state: ChatAgentState, config: RunnableConfig) -> dict:
    """Call the LLM. Returns AIMessage (with or without tool_calls)."""
    from chat.views import execute_llm
    from chat.extraction import extract_tool_calls, is_tool_plan_json
    import chat.tools as shared_tools

    conf = config or {}
    callback = conf.get("configurable", {}).get("stream_callback")
    attachments = conf.get("configurable", {}).get("attachments", [])
    provider = state["provider"]
    model = state["model"]
    iteration = _count_ai_messages(state["messages"])
    at_limit = iteration >= state.get("max_iterations", 30) - 1

    # ── Build prompt ──
    # The *latest* human message is this turn's question.
    #
    # This used to take the first one and break. The checkpointer is keyed by
    # thread_id = chat session id and `messages` uses the add_messages reducer,
    # so a new run on an existing session appends to the stored list rather than
    # replacing it — meaning "first HumanMessage" is the first thing the user
    # ever said in that conversation. Every turn after the first therefore
    # re-answered the opening question, ignoring what was actually asked.
    original_prompt = ""
    for msg in reversed(state["messages"]):
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

    # Don't pass tools if at iteration limit (forces final answer).
    # Tool list = built-in + MCP tools enabled for this user.
    if at_limit:
        tools_payload = None
    else:
        tools_payload = await shared_tools.get_available_tools(
            state.get("user_id"), memory_enabled=state.get("memory_enabled", True)
        )

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
                attachments=attachments,
                stream=True,
            )
            # execute_llm returns a plain dict (not an async stream) on an error
            # path — e.g. provider unavailable or the user has no verified
            # credential. Surface it gracefully instead of crashing on `async for`.
            if isinstance(stream_gen, dict):
                content = stream_gen.get("content") or (
                    "The selected LLM provider is unavailable. Add and verify a "
                    "provider credential (e.g. OpenRouter) in Settings to use chat."
                )
                logger.warning(f"[Agent Node] LLM unavailable (no stream): {content}")
            else:
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
                            if not is_tool_plan_json(chunk["content"]):
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
            logger.warning("[Agent Node] LLM stream timed out")
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
                    attachments=attachments,
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

async def tools_node(state: ChatAgentState, config: RunnableConfig) -> dict:
    """Execute tool calls from the last AIMessage."""
    import chat.tools as shared_tools
    from chat.views import perform_image_search, _sanitize_tool_args

    last_msg = state["messages"][-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"messages": []}

    conf = config or {}
    callback = conf.get("configurable", {}).get("stream_callback")
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

        # --- HITL CHECK ---
        if fn in shared_tools.SENSITIVE_TOOLS:
            # Check if this specific call_id has already been approved in metadata
            approved_calls = metadata.get("approved_tool_calls", [])
            if call_id not in approved_calls:
                logger.info(f"[Tools Node] Interrupting for sensitive tool: {fn}")
                
                # Create Notification
                try:
                    from notifications.utils import create_notification
                    from core.models import User
                    from asgiref.sync import sync_to_async
                    
                    @sync_to_async
                    def send_hitl_notification():
                        user = User.objects.get(id=state["user_id"])
                        create_notification(
                            user=user,
                            type='hitl_request',
                            title='Permission Required',
                            message=f'The AI agent requires your permission to run the tool: {fn}',
                            data={'tool': fn, 'args': args, 'thread_id': state.get("thread_id")}
                        )
                    # We are in an async function, so we can just fire and forget or await
                    asyncio.create_task(send_hitl_notification())
                except Exception as e:
                    logger.error(f"Failed to create HITL notification: {e}")

                if callback:
                    await callback("ask_permission", {
                        "tool": fn,
                        "args": args,
                        "call_id": call_id,
                    })
                interrupt(f"Permission required for {fn}")

        args = _sanitize_tool_args(args)
        if fn == "web_search" and not args.get("query"):
            args["query"] = clean_content
        # Guard: an empty query is the common failure for the history search, and
        # the model recovers if told exactly which argument was missing.
        if fn == "search_conversation_history" and not args.get("query"):
            tool_messages.append(ToolMessage(
                content=(
                    "Error: The 'query' argument was empty. Provide the keywords to look for, "
                    "e.g. {\"query\": \"budget figure spreadsheet\"}."
                ),
                tool_call_id=call_id,
                name=fn,
            ))
            continue

        # Trace
        thought_ctx = thinking.strip()[-150:] if thinking else ""
        trace_entry = {"tool": fn, "args": args, "iteration": iteration, "thought": thought_ctx, "summary": thought_ctx}
        tool_trace.append(trace_entry)

        if callback:
            await callback("agent_trace", {"sub_type": "tool", "tool": fn, "args": args, "iteration": iteration, "thought": thought_ctx, "summary": thought_ctx})

        logger.info(f"[Tools Node iter={iteration}] Calling: {fn} | args keys: {list(args.keys())} | code_present: {'code' in args and bool(args.get('code'))}")

        # Execute.
        # thread_id is the ChatSession UUID (views sets it from str(session.id)).
        # search_conversation_history needs it to scope the lookup to this
        # conversation — without it the tool cannot tell whose history to read.
        ctx = {"user_id": state["user_id"], "session_id": state.get("thread_id")}
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
            elif fn == "render_html_artifact":
                parsed = json.loads(res)
                if parsed.get("type") == "html_artifact":
                    artifacts = metadata.get("html_artifacts", [])
                    artifacts.append({
                        "title": parsed.get("title"),
                        "html": parsed.get("html"),
                        "width": parsed.get("width"),
                        "height": parsed.get("height"),
                    })
                    metadata["html_artifacts"] = artifacts
                    if callback:
                        await callback("html_artifact", artifacts[-1])
            elif fn == "generate_image":
                parsed = json.loads(res)
                if parsed.get("status") == "success" and parsed.get("image_url"):
                    # Create Notification
                    try:
                        from notifications.utils import create_notification
                        from core.models import User
                        from asgiref.sync import sync_to_async
                        
                        @sync_to_async
                        def send_image_notification():
                            user = User.objects.get(id=state["user_id"])
                            create_notification(
                                user=user,
                                type='image_ready',
                                title='Image Generated',
                                message='Your requested image has been generated and is ready for viewing.',
                                data={'image_url': parsed["image_url"]}
                            )
                        asyncio.create_task(send_image_notification())
                    except Exception as e:
                        logger.error(f"Failed to create image notification: {e}")

                    imgs = metadata.get("images", [])
                    imgs.append({
                        "title": "AI Generated Image",
                        "image": parsed["image_url"],
                        "url": parsed["image_url"],
                        "source": "DALL-E 3",
                    })
                    metadata["images"] = imgs
                    metadata["has_generated_image"] = True
                    if callback:
                        await callback("images_update", {"images": imgs})
            elif fn == "knowledge_base_search":
                parsed = json.loads(res)
                if parsed.get("status") == "success":
                    metadata["kb_search_results"] = parsed.get("results", [])
                    if callback:
                        await callback("status", {"phase": "rag_results", "message": f"Found {parsed.get('count', 0)} relevant document chunks."})
            elif fn == "scrape_webpage":
                parsed = json.loads(res)
                if parsed.get("status") == "success":
                    if callback:
                        await callback("status", {"phase": "page_scraped", "message": f"Scraped {parsed.get('url', 'page')} successfully."})
            elif fn == "search_conversation_history":
                parsed = json.loads(res)
                if parsed.get("matches"):
                    # Surfaced so the UI can show *that* the assistant went back
                    # through the history — otherwise recalling something from 200
                    # turns ago looks like the model simply had it in context.
                    if callback:
                        await callback("status", {
                            "phase": "history_recall",
                            "message": f"Recalled {parsed.get('returned', 0)} earlier message(s) from this conversation.",
                        })
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
    
    # Add memory checkpointer
    memory = MemorySaver()
    return graph.compile(checkpointer=memory)


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
    thread_id: str,
    response_format: str,
    clean_content: str,
    intent: str,
    history_list: list,
    attachments: list | None = None,
    stream_callback=None,
    max_iterations: int = 30,
    memory_enabled: bool = True,
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

    # ── Handle Text-Only models ──
    # If the model doesn't support vision, we pre-extract text from attachments
    # and inject it into the prompt context so the agent doesn't need to 'see' them.
    vision_enabled = await _is_vision_enabled_model(model, provider)
    processed_attachments = []
    attachment_context = []
    
    if attachments:
        for att in attachments:
            if vision_enabled:
                processed_attachments.append(att)
            else:
                # Text-only model: extract content if possible
                try:
                    from inference.utils import extract_text_from_file
                    file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                    # Only extract if it's a document/text. Media gets a placeholder.
                    if att.file_type in ('image', 'video'):
                        attachment_context.append(f"### Attachment: {att.filename} ({att.file_type.capitalize()})\n[Note: This model does not support visual input. You cannot see this {att.file_type} directly.]")
                    else:
                        # Use pre-extracted text if available, otherwise extract now
                        text = getattr(att, 'extracted_text', "")
                        if not text:
                            try:
                                from inference.utils import extract_text_from_file
                                file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                                text = await asyncio.to_thread(extract_text_from_file, file_path, att.file_type)
                            except Exception as e:
                                logger.warning(f"Failed to extract text from {att.filename}: {e}")
                        
                        if text:
                            # Cap to avoid context overflow (20k chars is usually enough for a preview)
                            preview = text[:20000]
                            attachment_context.append(f"### Attachment: {att.filename}\n{preview}")
                        else:
                            attachment_context.append(f"### Attachment: {att.filename}\n[Content could not be extracted directly.]")
                except Exception as e:
                    logger.warning(f"Failed to pre-extract text from attachment {att.filename}: {e}")
                    attachment_context.append(f"### Attachment: {att.filename}\n[Error during text extraction.]")

    # Inject attachment context if any was extracted
    if attachment_context:
        context_block = "\n\n## Uploaded Files Context\n" + "\n\n".join(attachment_context)
        full_prompt += context_block

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
        "thread_id": thread_id,
        "response_format": response_format,
        "clean_content": clean_content,
        "intent": intent,
        "history_list": history_list,
        "max_iterations": max_iterations,
        "memory_enabled": memory_enabled,
    }

    config = {
        "configurable": {
            "thread_id": thread_id,
            "stream_callback": stream_callback,
            "attachments": processed_attachments,
        },
        "recursion_limit": max_iterations * 2 + 10,
    }

    interrupted = False
    try:
        # Resume ONLY when the graph is genuinely paused mid-run — i.e. it stopped
        # at an interrupt() awaiting HITL approval.
        #
        # `.values` alone is the wrong test, and was the bug: thread_id is the
        # chat session id, so every turn after the first finds leftover values
        # from the previous turn and takes the resume branch. Resuming a graph
        # that already reached END re-emits its terminal state, so the user's new
        # message was never looked at and they got the *previous* answer back.
        # It hid in dev because MemorySaver is per-process and a reload clears it;
        # under a long-lived ASGI worker it would hit on the 2nd message of every
        # conversation.
        #
        # `.next` is the reliable signal: non-empty means there are still nodes
        # pending (paused), empty means the run finished.
        state_snapshot = await chat_agent_graph.aget_state(config)
        if state_snapshot.values and state_snapshot.next:
            logger.info(f"[LangGraph] Resuming interrupted thread {thread_id} (pending: {state_snapshot.next})")
            result = await chat_agent_graph.ainvoke(None, config=config)
        else:
            logger.info(f"[LangGraph] Starting new run on thread {thread_id}")
            result = await chat_agent_graph.ainvoke(initial_state, config=config)

    except Exception as e:
        if "Permission required" in str(e):
            interrupted = True
            # Get the current state values to return partial results
            state_snapshot = await chat_agent_graph.aget_state(config)
            result = state_snapshot.values
        else:
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
