import asyncio
from typing import Any

# Timeout constants for agentic loop (in seconds)
LLM_STREAM_TIMEOUT = 180  # Max time to wait for LLM stream (in seconds)
TOOL_EXECUTION_TIMEOUT = 120  # Max time to wait for tool execution
MAX_THINKING_CHUNKS = 100000  # Max thinking chunks before forcing exit


def normalize_llm_payload(raw: Any, provider: str, model: str, tool_context_text: str | None, metadata: dict | None = None, llm_result: dict | None = None, max_followups: int = 3) -> dict:
    """
    Normalize LLM output into canonical payload dict.

    Returns keys: response, follow_ups, thinking, summary, sources, images, videos, tool_trace, metadata
    """
    if metadata is None:
        metadata = {}

    # First, try to parse structured fields from raw using existing parser
    try:
        content, follow_ups, thinking, summary = parse_llm_json_response(raw)
    except Exception:
        # Fallback: treat raw as plain text
        content, follow_ups, thinking, summary = (str(raw) if raw is not None else ""), [], "", ""

    # Extra safety: If content starts with { and looks like a raw JSON error we somehow missed
    if content.strip().startswith('{') and content.strip().endswith('}'):
        try:
            potential_json = json.loads(content.strip())
            if isinstance(potential_json, dict) and (potential_json.get('status') == 'error' or potential_json.get('Error') or potential_json.get('error')):
                err_msg = potential_json.get('error') or potential_json.get('Error') or potential_json.get('message') or "An internal tool execution error occurred."
                content = f"I encountered an issue while trying to process your request: {err_msg}"
        except:
            pass

    # Ensure types
    content = content or ""
    follow_ups = follow_ups or []
    
    # If JSON didn't have thinking, check if we already have it from the stream in metadata
    if not thinking and metadata:
        thinking = metadata.get('thinking', '')
    
    thinking = thinking or ""
    summary = summary or ""

    # Cap follow-ups
    follow_ups = [str(f) for f in follow_ups][:max_followups]

    # Sources / images / videos may already be in metadata from tool runs
    sources = metadata.get('sources', []) if isinstance(metadata.get('sources', []), list) else []
    images = metadata.get('images', []) if isinstance(metadata.get('images', []), list) else []
    videos = metadata.get('videos', []) if isinstance(metadata.get('videos', []), list) else []

    # Summarize tool trace if present
    raw_tool_trace = metadata.get('tool_trace') or []
    summarized_trace = []
    if isinstance(raw_tool_trace, list):
        for t in raw_tool_trace[-20:]:
            try:
                trace_summary = t.get('summary') if isinstance(t, dict) and t.get('summary') else None
                if not trace_summary:
                    # create short summary from args/results
                    args = t.get('args') if isinstance(t, dict) else None
                    trace_summary = (str(args)[:200]) if args else ''
                summarized_trace.append({
                    'tool': t.get('tool') if isinstance(t, dict) else str(t),
                    'iteration': t.get('iteration') if isinstance(t, dict) else None,
                    'summary': trace_summary,
                })
            except Exception:
                continue

    # If tool_context_text exists but no structured sources, inject a preview
    raw_preview = (str(raw) or '')[:2000]

    out_meta = {
        'provider': provider,
        'model': model,
        'tokens': metadata.get('tokens') if isinstance(metadata.get('tokens'), int) else None,
        'raw_llm_output_preview': raw_preview,
        'summary': summary,
    }

    payload = {
        'response': content,
        'follow_ups': follow_ups,
        'thinking': thinking,
        'summary': summary,
        'sources': sources,
        'images': images,
        'videos': videos,
        'tool_trace': summarized_trace,
        'metadata': out_meta,
    }

    return payload

"""
Standalone AI Chat Views — Perplexity-Style

Features:
- Dynamic LLM provider routing via the node registry
- Smart conversation history with 100K token truncation
- Intent-aware search (DuckDuckGo)
- Citations and follow-up questions in AI responses
- Workflow suggestion from chat
- File upload endpoint
- /image and /video slash commands
"""
from django.shortcuts import get_object_or_404
from rest_framework import status, viewsets
from adrf.decorators import api_view
from rest_framework.decorators import permission_classes, parser_classes, action, api_view as sync_api_view
from rest_framework.permissions import IsAuthenticated
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.response import Response
from uuid import UUID, uuid4

from .models import ChatSession, ChatMessage, ChatAttachment
from .serializers import ChatSessionSerializer, ChatMessageSerializer, ChatAttachmentSerializer
from .extraction import extract_tool_calls, strip_tool_calls, get_block_signatures, parse_tool_arguments, fuzzy_json_loads
import logging
import asyncio
import json
import re
import datetime
import urllib.request
from urllib.parse import urlparse
from asgiref.sync import sync_to_async

logger = logging.getLogger(__name__)

# Re-assign or use standard names to avoid NameError if code uses _re_module
_re_module = re

def _sanitize_tool_args(args: dict) -> dict:
    """
    Strip residual XML/HTML tags from tool call argument values.
    LLMs sometimes hallucinate tool calls wrapped in XML like:
      <tool_arg><query>DeepSeek...</query></tool_arg>
    This pollutes both the search query sent to external APIs AND the UI display.
    
    NOTE: 'code' values are exempt — stripping XML-like patterns would destroy
    valid Python code containing type hints, comparisons, HTML strings, etc.
    """
    if not isinstance(args, dict):
        args = parse_tool_arguments(args)
        if not isinstance(args, dict):
            return {}
    cleaned = {}
    for k, v in args.items():
        if isinstance(v, str) and k != "code":
            # Strip ALL XML/HTML-like tags from values (except code)
            v = _re_module.sub(r'</?[a-zA-Z_][a-zA-Z0-9_:.-]*[^>]*>', '', v)
            v = v.strip()
        cleaned[k] = v
    return cleaned


def has_unresolved_tool_syntax(text: str) -> bool:
    """
    Detect likely tool-call intent when structured tool parsing failed.
    Excludes content inside code blocks to avoid false positives on code containing
    strings like "invoke", "tool_call", etc.
    """
    if not isinstance(text, str) or not text.strip():
        return False

    # Strip code blocks first to avoid false positives on code content
    text_without_code = _re_module.sub(r'```[\s\S]*?```', '', text)
    text_without_code = _re_module.sub(r'`[^`]+`', '', text_without_code)

    lowered = text_without_code.lower()
    hard_markers = [
        "<functioncall>",
        "</functioncall>",
        "<tool_call>",
        "</tool_call>",
        "<invoke",
        "</invoke>",
        "action input:",
        "selected tool:",
        ":tool_call",
    ]
    return any(marker in lowered for marker in hard_markers)


def has_structured_final_response(text: str) -> bool:
    """
    Require a valid final JSON payload with a non-empty "response" field.
    This keeps the agentic loop running until we have a real final answer object.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    raw = text.strip()

    # Unwrap explicit wrappers first.
    tag_match = _re_module.search(r'<json_response>(.*?)</json_response>', raw, _re_module.DOTALL | _re_module.IGNORECASE)
    if tag_match:
        raw = tag_match.group(1).strip()
    else:
        fence_match = _re_module.search(r'```(?:json)?\s*\n?(.*?)\n?```', raw, _re_module.DOTALL | _re_module.IGNORECASE)
        if fence_match:
            raw = fence_match.group(1).strip()

    # If mixed prose + json, extract the first balanced json object.
    if not raw.startswith("{"):
        start = raw.find("{")
        if start != -1:
            depth = 0
            in_str = False
            esc = False
            buf = ""
            for ch in raw[start:]:
                buf += ch
                if ch == '"' and not esc:
                    in_str = not in_str
                if not in_str:
                    if ch == "{":
                        depth += 1
                    elif ch == "}":
                        depth -= 1
                        if depth == 0:
                            break
                esc = (ch == '\\' and not esc)
            if depth == 0 and buf:
                raw = buf

    try:
        data = json.loads(raw, strict=False)
        if isinstance(data, dict):
            resp = data.get("response")
            if isinstance(resp, str) and bool(resp.strip()):
                return True
    except Exception:
        pass

    # Fallback to prevent overthinking loops if it's text.
    # Accept text as final if it's long enough and doesn't look like
    # intermediate action narration ("I'll search for...", "Let me check...").
    if len(text.strip()) > 10 and not looks_like_intermediate_action_text(text):
        return True

    return False


def looks_like_intermediate_action_text(text: str) -> bool:
    """
    Detect non-final action narration like "I'll search for..." that should not be
    persisted as the assistant's final answer.
    """
    if not isinstance(text, str):
        return False
    lowered = text.strip().lower()
    if not lowered:
        return False
    return bool(
        _re_module.search(
            r"\b(i(?:'| )?ll|i will|let me)\s+(search|look up|use|fetch|check|call|find|try|review|think|double check|verify)\b",
            lowered,
            _re_module.IGNORECASE,
        )
    )

@sync_to_async
def serialize_message(msg):
    return ChatMessageSerializer(msg).data

@sync_to_async
def serialize_attachment(att):
    return ChatAttachmentSerializer(att).data

# ==================== Constants ====================
from workflow_backend.thresholds import (
    MAX_CONTEXT_TOKENS, HISTORY_WINDOW, SEARCH_RESULT_LIMIT,
    ASSISTANT_SUMMARY_WORD_LIMIT, FLASH_SUMMARY_CHAR_LIMIT,
    IS_LARGE_FILE_THRESHOLD, LARGE_FILE_PREVIEW_LENGTH, DOCUMENT_EXTRACT_CAP,
    IMAGE_SEARCH_MAX_RESULTS, VIDEO_SEARCH_MAX_RESULTS,
    MAX_TOOL_ITERATIONS
)

# Provider slug -> node_type mapping
PROVIDER_NODE_MAP = {
    'openai': 'openai',
    'gemini': 'gemini',
    'ollama': 'ollama',
    'openrouter': 'openrouter',
    'perplexity': 'perplexity',
    'huggingface': 'huggingface',
    'anthropic': 'anthropic',
    'deepseek': 'deepseek',
    'xai': 'xai',
}


# ==================== Token Estimation ====================
def estimate_tokens(text: str) -> int:
    """Approximate token count. ~4 chars per token for English."""
    return len(text) // 4


def build_history_prompt(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> str:
    """
    Build a conversation history string from messages, 
    truncating oldest messages to stay under the token limit.
    """
    _, history_prompt = build_history_payload(messages, max_tokens_list=max_tokens, max_tokens_prompt=max_tokens)
    return history_prompt

def build_history_list(messages: list, max_tokens: int = MAX_CONTEXT_TOKENS) -> list[dict]:
    """
    Build a structured conversation history list from messages, 
    truncating oldest messages to stay under the token limit.
    Returns format: [{"role": "user"|"assistant", "content": "..."}]
    
    [TWO-STAGE FEEDING]: If an assistant message has a summary, use it instead of full content.
    """
    history_list, _ = build_history_payload(messages, max_tokens_list=max_tokens, max_tokens_prompt=max_tokens)
    return history_list


def build_history_payload(
    messages: list,
    max_tokens_list: int = MAX_CONTEXT_TOKENS,
    max_tokens_prompt: int = MAX_CONTEXT_TOKENS,
) -> tuple[list[dict], str]:
    """
    Build both structured history and prompt history in one pass.
    """
    history_list: list[dict] = []
    history_parts: list[str] = []
    list_tokens = 0
    prompt_tokens = 0

    # Process newest-first and then reverse to keep chronological order.
    for msg in reversed(messages):
        role = getattr(msg, 'role', 'user')
        content = getattr(msg, 'content', '') or ''

        if role == 'user':
            role_label = "User"
        elif role == 'system':
            role_label = "System/Context"
        else:
            role_label = "Assistant"
        prompt_entry = f"{role_label}: {content}"
        prompt_entry_tokens = estimate_tokens(prompt_entry)
        if prompt_tokens + prompt_entry_tokens <= max_tokens_prompt:
            history_parts.append(prompt_entry)
            prompt_tokens += prompt_entry_tokens

        if role not in ['user', 'assistant']:
            continue

        if role == 'assistant' and msg.metadata and msg.metadata.get('summary'):
            list_content = (
                f"[SUMMARY (ID: {msg.id})]: {msg.metadata['summary']}\n\n"
                f"(Use get_chat_message_full_text(message_id={msg.id}) to see the full content if needed.)"
            )
        else:
            list_content = content

        list_entry_tokens = estimate_tokens(list_content)
        if list_tokens + list_entry_tokens <= max_tokens_list:
            history_list.append({"role": role, "content": list_content})
            list_tokens += list_entry_tokens

        # Stop once both budgets are full.
        if list_tokens >= max_tokens_list and prompt_tokens >= max_tokens_prompt:
            break

    history_list.reverse()
    history_parts.reverse()
    return history_list, "\n\n".join(history_parts)


# ==================== Intent Classification ====================
def classify_intent(content: str) -> tuple[str, str]:
    """
    Classify user intent from message content.
    Returns (intent, clean_content)
    
    Intents: chat, search, image, video, workflow, coding
    """
    content_stripped = content.strip()
    
    # Explicit slash commands
    if content_stripped.startswith('/search '):
        return 'search', content_stripped[8:].strip()
    if content_stripped.startswith('/image '):
        return 'image', content_stripped[7:].strip()
    if content_stripped.startswith('/video '):
        return 'video', content_stripped[7:].strip()
    if content_stripped.startswith('/research '):
        return 'research', content_stripped[10:].strip()
    if content_stripped.startswith('/workflow '):
        return 'workflow', content_stripped[10:].strip()
    if content_stripped.startswith('/coding '):
        return 'coding', content_stripped[8:].strip()
    
    # Heuristic-based implicit search detection
    search_indicators = [
        'what is', 'who is', 'when did', 'how to', 'latest', 'current',
        'news about', 'tell me about', 'search for', 'look up', 'find',
        'what are the', 'define', 'explain', 'compare',
    ]
    content_lower = content_stripped.lower()
    for indicator in search_indicators:
        if content_lower.startswith(indicator):
            return 'search', content_stripped
    
    return 'chat', content_stripped


def resolve_agent_iteration_limit(intent: str) -> int:
    """
    Decide a bounded but generous iteration cap for the agentic tool loop.
    """
    base_limit = max(MAX_TOOL_ITERATIONS, 12)
    if intent in ('research', 'search'):
        # Allow deeper exploration for search-heavy flows while staying bounded.
        return min(40, max(base_limit * 3, 24))
    return min(30, max(base_limit * 2, 12))


@sync_to_async
def _fetch_enriched_history_messages(session: ChatSession, excluded_message_id, supports_docs: bool = False) -> list[ChatMessage]:
    """
    Load recent history and enrich messages with attachment/source context.
    """
    msgs = list(
        ChatMessage.objects.filter(session=session)
        .exclude(id=excluded_message_id)
        .order_by('-created_at')[:HISTORY_WINDOW]
    )
    msgs.reverse()

    seen_attachments: dict[str, bool] = {}
    assistant_seen_after = False
    for m in reversed(msgs):
        aid = m.metadata.get('attachment_id') if m.metadata else None
        if aid:
            seen_attachments[aid] = assistant_seen_after
        if m.role == 'assistant':
            assistant_seen_after = True

    research_turns: list[int] = []
    for i, m in enumerate(msgs):
        if m.role == 'assistant' and m.metadata and m.metadata.get('sources'):
            research_turns.append(i)

    research_with_snippets = research_turns[-3:] if len(research_turns) > 3 else research_turns

    if seen_attachments:
        attachment_ids: list[UUID] = []
        for aid in seen_attachments.keys():
            try:
                attachment_ids.append(UUID(aid))
            except (ValueError, TypeError):
                continue

        att_map: dict[str, tuple[str, str]] = {}
        if attachment_ids:
            att_map = {str(a.id): (a.filename, a.extracted_text) for a in ChatAttachment.objects.filter(id__in=attachment_ids)}

        for m in msgs:
            aid = m.metadata.get('attachment_id') if m.metadata else None
            if not aid:
                continue
            if aid in att_map:
                fname, full_txt = att_map[aid]
                if not full_txt:
                    continue
                if supports_docs:
                    m.content = f"{m.content}\n\n[RESOURCE: {fname} (Multimodal Injection Enabled)]"
                    continue
                if not seen_attachments[aid]:
                    m.content = f"{m.content}\n\n[FULL RESOURCE CONTENT: {fname}]\n{full_txt}\n[END FULL CONTENT]"
                else:
                    preview = full_txt[:LARGE_FILE_PREVIEW_LENGTH]
                    m.content = (
                        f"{m.content}\n\n"
                        f"[RESOURCE: FILE] - ID: {aid}\n"
                        f"Preview: {preview}...\n"
                        f"[SYSTEM NOTE: You previously read the full text of \"{fname}\". "
                        f"For deep citations, use `read_attachment_text` tool with ID {aid}.]"
                    )
            else:
                m.content = (
                    f"{m.content}\n\n"
                    f"[RESOURCE DELETED: The file referenced here (ID: {aid}) is no longer available.]"
                )

    for i, m in enumerate(msgs):
        if i not in research_turns:
            continue
        sources = m.metadata.get('sources', []) if m.metadata else []
        if not sources:
            continue
        show_snippets = (i in research_with_snippets)
        source_blocks: list[str] = []
        for s in sources[:8]:
            title = s.get('title', 'Source')
            url = s.get('url', '#')
            if show_snippets:
                snip = s.get('snippet', 'No snippet')[:500]
                source_blocks.append(f"- [{title}]({url}) Content: {snip}")
            else:
                source_blocks.append(f"- [{title}]({url}) (Reference only - Older history)")

        sources_txt = "\n".join(source_blocks)
        m.content = (
            f"{m.content}\n\n"
            f"<context_metadata type=\"historical_research\">\n"
            f"[RESEARCH_LOG - PREVIOUS ROUNDS]\n"
            f"{sources_txt}\n"
            f"[SYSTEM NOTE: These sources were already scanned. Use `read_url` if you need the full text again. "
            f"Do not repeat this log in your response.]\n"
            f"</context_metadata>"
        )

    return msgs


async def prepare_history_context(
    session: ChatSession,
    excluded_message_id,
    supports_docs: bool = False,
    list_token_budget: int = MAX_CONTEXT_TOKENS - 4000,
    prompt_token_budget: int = MAX_CONTEXT_TOKENS,
) -> tuple[list[ChatMessage], list[dict], str]:
    history_messages = await _fetch_enriched_history_messages(
        session=session,
        excluded_message_id=excluded_message_id,
        supports_docs=supports_docs,
    )
    history_list, history_prompt = build_history_payload(
        history_messages,
        max_tokens_list=list_token_budget,
        max_tokens_prompt=prompt_token_budget,
    )
    return history_messages, history_list, history_prompt


def append_history_message(history_messages: list[ChatMessage], message: ChatMessage, max_window: int = HISTORY_WINDOW) -> list[ChatMessage]:
    """
    Append-only helper to avoid rebuilding history lists inside long request flows.
    """
    if not history_messages:
        return [message]
    next_history = history_messages + [message]
    if len(next_history) > max_window:
        next_history = next_history[-max_window:]
    return next_history


def resolve_provider_model(payload: dict, session: ChatSession) -> tuple[str, str]:
    provider = payload.get('llm_provider') or session.llm_provider
    model = payload.get('llm_model') or session.llm_model
    return provider, model


async def sync_session_model_overrides(payload: dict, session: ChatSession, provider: str, model: str) -> None:
    if (payload.get('llm_provider') or payload.get('llm_model')) and (
        provider != session.llm_provider or model != session.llm_model
    ):
        session.llm_provider = provider
        session.llm_model = model
        await session.asave(update_fields=['llm_provider', 'llm_model'])


def current_time_string() -> str:
    try:
        from django.utils import timezone
        return timezone.now().strftime("%A, %B %d, %Y %I:%M %p %Z")
    except Exception:
        return datetime.datetime.now().strftime("%A, %B %d, %Y %I:%M %p")


def parse_first_json_object(text: Any) -> dict:
    """
    Safely parse the first JSON object found in text.
    """
    if not isinstance(text, str):
        return {}
    try:
        start = text.find('{')
        end = text.rfind('}')
        if start != -1 and end != -1 and end >= start:
            return json.loads(text[start:end + 1])
    except Exception:
        return {}
    return {}


def normalize_research_queries(plan_json: dict, fallback_query: str) -> list[str]:
    queries = plan_json.get('queries', [fallback_query]) if isinstance(plan_json, dict) else [fallback_query]
    if not isinstance(queries, list):
        return [fallback_query]
    return queries or [fallback_query]


def normalize_total_links(raw_value: Any, default: int = 15, min_value: int = 5, max_value: int = 50) -> int:
    try:
        return max(min_value, min(int(raw_value), max_value))
    except Exception:
        return default


async def run_eager_search_intent(clean_content: str, user_id: int, shared_tools) -> dict:
    """
    Execute eager web search + image search and return normalized payload.
    """
    res = await shared_tools.execute_tool("web_search", {"query": clean_content}, {"user_id": user_id})
    result = {
        "search_query": clean_content,
        "sources": [],
        "images": [],
        "eager_text": "",
        "raw_result": res,
    }
    try:
        parsed_res = json.loads(res)
    except Exception:
        parsed_res = None

    if parsed_res and parsed_res.get("type") == "search_results":
        result["sources"] = parsed_res.get("sources", [])[:50]
        result["eager_text"] = parsed_res.get("text", "")
    return result


async def run_workflow_suggestion_intent(clean_content: str, user_id: int, shared_tools) -> dict:
    """
    Execute eager workflow suggestion and return normalized payload.
    """
    res = await shared_tools.execute_tool("suggest_workflow", {"intent": clean_content}, {"user_id": user_id})
    result = {"raw_result": res, "workflow_id": None, "workflow_name": None}
    try:
        parsed = json.loads(res)
        if parsed.get("found"):
            result["workflow_id"] = parsed.get("workflow_id")
            result["workflow_name"] = parsed.get("name")
    except Exception:
        pass
    return result


# ==================== DuckDuckGo Search ====================
async def perform_web_search(query: str, max_results: int = SEARCH_RESULT_LIMIT) -> dict:
    """
    Execute a web search using DuckDuckGo.
    Returns { results_text, sources }
    """
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        def _search():
            # v8.x of duckduckgo_search removed the 'backend' parameter.
            # Just call .text() without it. Retry once on failure.
            MAX_RETRIES = 2
            for attempt in range(MAX_RETRIES):
                try:
                    with DDGS(timeout=15) as ddgs:
                        results = ddgs.text(
                            query,
                            region='us-en',
                            safesearch='moderate',
                            max_results=max_results,
                        )
                        res_list = list(results) if results else []
                        if res_list:
                            return res_list
                except Exception as e:
                    logger.warning(f"Web search attempt {attempt+1} failed: {e}")
            return []

        raw_results = await asyncio.to_thread(_search)

        if not raw_results:
            logger.info(f"No web results found for '{query}'")
            return {"results_text": "", "sources": []}

        text_parts = []
        sources = []
        for i, r in enumerate(raw_results):
            title = r.get('title', 'No Title')
            body = r.get('body', '')
            url = r.get('href', '') or r.get('url', '')  # Handle both possible keys

            if not url and not body:
                continue

            domain = ""
            if url:
                try:
                    domain = urlparse(url).hostname or ""
                except Exception:
                    domain = ""

            thumbnail = (
                r.get('thumbnail')
                or r.get('image')
                or r.get('icon')
                or r.get('favicon')
                or r.get('photo')
                or r.get('img')
                or ""
            )
            favicon = r.get('favicon') or (f"https://www.google.com/s2/favicons?domain={domain}&sz=64" if domain else "")

            text_parts.append(f"[{i+1}] {title}\n{body}")
            sources.append({
                "title": title,
                "url": url,
                "snippet": body,
                "source": domain,
                "publisher": domain,
                "thumbnail": thumbnail,
                "favicon": favicon,
            })

        return {
            "results_text": "\n\n".join(text_parts),
            "sources": sources,
        }
    except Exception as e:
        logger.error(f"Web search failed: {e}")
        return {"results_text": f"Web search failed: {str(e)}", "sources": []}

async def perform_image_search(query: str, max_results: int = IMAGE_SEARCH_MAX_RESULTS) -> dict:
    """Execute an image search using DuckDuckGo."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        def _search():
            for attempt in range(2):
                try:
                    with DDGS(timeout=10) as ddgs:
                        results = ddgs.images(query, max_results=max_results)
                        return list(results) if results else []
                except Exception as e:
                    logger.warning(f"Image search failed: {e}")
            return []

        raw_results = await asyncio.to_thread(_search)
        logger.info(f"Image search for '{query}' returned {len(raw_results) if raw_results else 0} results")
        if not raw_results:
            return {"images": []}

        images = []
        for r in raw_results:
            images.append({
                "title": r.get("title", ""),
                "image": r.get("image", ""),
                "url": r.get("url", ""),
                "source": r.get("source", "")
            })
        return {"images": images}
    except Exception as e:
        logger.error(f"Image search crashed: {e}")
        return {"images": []}

async def perform_video_search(query: str, max_results: int = VIDEO_SEARCH_MAX_RESULTS) -> dict:
    """Execute a video search using DuckDuckGo."""
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS

        def _search():
            for attempt in range(2):
                try:
                    with DDGS(timeout=10) as ddgs:
                        results = ddgs.videos(query, max_results=max_results)
                        return list(results) if results else []
                except Exception:
                    pass
            return []

        raw_results = await asyncio.to_thread(_search)
        logger.info(f"Video search for '{query}' returned {len(raw_results) if raw_results else 0} results")
        if not raw_results:
            return {"videos": []}

        videos = []
        for r in raw_results:
            url = r.get("content", "") or ""
            # Some video results return bare youtube IDs or HTTP paths
            if url and not url.startswith('http'):
                 url = "https://www.youtube.com/watch?v=" + url
            videos.append({
                "title": r.get("title", ""),
                "description": r.get("description", ""),
                "url": url,
                "duration": r.get("duration", ""),
                "publisher": r.get("publisher", "")
            })
        return {"videos": videos}
    except Exception as e:
        logger.error(f"Video search crashed: {e}")
        return {"videos": []}


async def scrape_sources(
    sources: list[dict],
    per_source_char_limit: int = 4000,
    min_content_chars: int = 100,
    ) -> tuple[list[str], list[dict]]:
    """
    Fetch and extract text for source URLs, returning extracted blocks and valid source metadata.
    """
    from .tools import validate_url_for_ssrf

    def _scrape_url(url: str) -> str:
        # SSRF protection: validate URL before fetching
        is_safe, _err = validate_url_for_ssrf(url)
        if not is_safe:
            return ""
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            html = urllib.request.urlopen(req, timeout=5).read()
            try:
                from bs4 import BeautifulSoup
                return BeautifulSoup(html, 'html.parser').get_text(separator=' ', strip=True)[:per_source_char_limit]
            except Exception:
                return html.decode('utf-8', errors='ignore')[:per_source_char_limit]
        except Exception:
            return ""

    urls = [s.get("url") for s in sources if s.get("url")]
    loop = asyncio.get_event_loop()
    scraped_contents = await asyncio.gather(
        *[loop.run_in_executor(None, _scrape_url, url) for url in urls],
        return_exceptions=True,
    )

    extracted_blocks: list[str] = []
    valid_sources: list[dict] = []
    for src, txt in zip(sources, scraped_contents):
        if isinstance(txt, str) and len(txt) > min_content_chars:
            extracted_blocks.append(f"Source [{src['title']}]({src['url']}):\n{txt}")
            valid_sources.append(src)
    return extracted_blocks, valid_sources


# ==================== Prompt Engineering ====================
def get_format_instructions(provider: str, model: str) -> str:
    """
    Returns the most effective formatting instructions for the given model.
    Strong models get native JSON instructions.
    Weaker models get reinforced Tag-based instructions to improve parsing.
    """
    model_lower = model.lower()
    
    # Models that struggle with raw JSON in long context or have specific quirks
    needs_reinforced_json = any(x in model_lower for x in ['llama-3', 'mistral', 'phi', 'gemma', 'qwen'])
    
    if needs_reinforced_json:
        return (
            "\n\n### CRITICAL: RESPONSE FORMATTING (STRICT JSON) ###\n"
            "You MUST output your final answer as a JSON object wrapped in <json_response> tags.\n"
            "DO NOT add any conversational filler, intro, or outro outside these tags.\n\n"
            "STRUCTURE:\n"
            "<json_response>\n"
            "{\n"
            '  "response": "Your markdown answer here...",\n'
            '  "summary": "A 1-2 sentence quick summary...",\n'
            '  "follow_ups": ["Q1", "Q2", "Q3"]\n'
            "}\n"
            "</json_response>\n\n"
            "Directives:\n"
            "1. The 'response' field MUST use clean Markdown (headers, bold, lists).\n"
            "2. ALWAYS use language-specific code blocks (e.g. ```python) for code.\n"
            "3. Provide exactly 3 engaging follow-up questions.\n"
            "4. You MUST escape all newlines as \\n inside JSON string values. Do not use literal newlines."
        )
    
    # Standard instruction for frontier models (GPT-4, Claude 3.5+, Gemini 1.5+)
    return (
        "\n\n### FINAL RESPONSE FORMATTING (MANDATORY JSON) ###\n"
        "Your ENTIRE output must be a single valid JSON object. "
        "DO NOT include any conversational preamble, intro, or closing remarks outside the JSON.\n\n"
        "STRUCTURE:\n"
        "{\n"
        '  "response": "Your full detailed answer with markdown formatting here.",\n'
        '  "summary": "A concise 1-2 sentence overview of the answer for quick reading.",\n'
        '  "follow_ups": ["Question 1?", "Question 2?", "Question 3?"]\n'
        "}\n\n"
        "GUIDELINES:\n"
        "- Use clean Markdown (lists, bold, headers) in the 'response' field.\n"
        "- Use language-specific code blocks (e.g., ```python) for all code.\n"
        "- Provide exactly 3 concise and engaging follow-up questions.\n"
        "- DO NOT wrap the JSON block in markdown code fences (```json). Just output the raw object.\n"
        "- You MUST escape all newlines as \\n inside JSON string values. Do not use literal newlines."
    )


async def get_interruption_context(session) -> dict:
    """
    Check session message history for previous interruptions and return context.
    
    Returns a dict with:
        - count: number of previous interruptions
        - reason: most recent interruption reason
        - iterations_used: iterations used in most recent interruption
    """
    if not session:
        return {'count': 0}
    
    # Check recent assistant messages for interruption metadata
    from asgiref.sync import sync_to_async
    
    @sync_to_async
    def _get_interruption_count():
        # Get last 10 assistant messages to check for interruptions
        recent_messages = list(
            ChatMessage.objects.filter(session=session, role='assistant')
            .order_by('-created_at')[:10]
        )
        
        interruption_count = 0
        last_reason = None
        last_iterations = None
        
        for msg in recent_messages:
            if msg.metadata and isinstance(msg.metadata, dict):
                if msg.metadata.get('interrupted'):
                    interruption_count += 1
                    if not last_reason:
                        last_reason = msg.metadata.get('partial_results', 'excessive processing')[:100]
                    if not last_iterations:
                        last_iterations = msg.metadata.get('iterations_completed')
        
        return {
            'count': interruption_count,
            'reason': last_reason,
            'iterations_used': last_iterations
        }
    
    return await _get_interruption_count()


def build_augmented_system_message(session, current_time: str, intent: str, provider: str, model: str, interruption_context: dict = None) -> str:
    """
    Constructs a unified, highly-directive system message with industry-standard rules.
    
    Args:
        session: ChatSession object
        current_time: Current timestamp string
        intent: Intent category (chat, search, research, etc.)
        provider: LLM provider name
        model: LLM model name
        interruption_context: Optional dict with keys 'count', 'reason', 'iterations_used' for optimization hints
    """
    base = session.system_prompt or "You are a helpful, knowledgeable AI assistant. Be concise but thorough."
    
    context_block = (
        f"\n\n### SYSTEM CONTEXT ###"
        f"\n- Current Date/Time: {current_time}"
        f"\n- Knowledge Cutoff: Your training data is not up-to-date. Assume you don't know recent events."
    )
    
    # Inject Buddy Screen Context
    from django.core.cache import cache
    buddy_context = cache.get(f"buddy_context_{session.user.id}")
    if buddy_context:
        context_block += f"\n\n### SCREEN CONTEXT (BUDDY MODE) ###\n"
        context_block += f"The user is currently looking at: {buddy_context.get('title', 'Unknown Page')} (URL: {buddy_context.get('url', '')})\n"
        context_block += "Visible interactive elements:\n"
        for item in buddy_context.get('interactables', [])[:100]:
            context_block += f"- [{item.get('buddy_id')}] <{item.get('tag')}> {item.get('text', '')[:100]} (type: {item.get('type') or 'N/A'})\n"
        context_block += "You can use 'frontend_click', 'frontend_fill', or 'frontend_navigate' tools to interact with the screen. Always use the provided buddy_id for elements."
    
    directives = (
        f"\n\n### CORE OPERATING RULES ###"
        f"\n1. ANTI-HALLUCINATION: Never fabricate facts, dates, or URLs. If unsure, you MUST call 'web_search'."
        f"\n2. REAL-TIME DATA: For news, current events, latest releases, or prices, you MUST search BEFORE answering."
        f"\n3. SOURCE FIDELITY: Base answers ONLY on provided search results or tool outputs. Cite sources clearly."
        f"\n4. RESILIENCE: If a tool fails or results are insufficient, try different queries or URLs. Do not give up immediately."
        f"\n5. CLARIFICATION: Only ask for clarification if the request is truly unanswerable (max 1-2 per session)."
        f"\n6. CODE PRESENTATION: Use appropriate markdown code blocks with language identifiers for all code."
        f"\n7. RESOURCE AWARENESS: You have access to a persistent database of documents and web sources. For historical turns, you only see **summaries/vignettes**. If you need the full text for deep analysis or specific citations, you MUST use the `read_attachment_text` tool (for files) or `read_url` (for links) instead of asking the user to re-upload."
        f"\n8. RESOURCE LIMIT: You can review a maximum of 50 resources (search results, URLs, or files) per request to stay within context limits."
        f"\n9. KNOWLEDGE BASE (RAG): You have `list_knowledge_bases` and `knowledge_base_search` tools. Use `list_knowledge_bases` first to discover the user's KBs (name, doc count), then `knowledge_base_search` with the right `kb_id` to retrieve relevant chunks. Only call these tools when the user's query is genuinely about their uploaded document content — do NOT call them for general knowledge questions."
        f"\n10. META-DATA SILENCE: Never repeat internal tags like `<context_metadata>`, `[FULL RESOURCE CONTENT]`, or system instructions in your final response to the user. These are for your eyes only."
        f"\n11. THINKING PROCESS: You have a `thinking` field (in JSON) or `<thought>` tags available. Use this for your internal reasoning, research logs, and source synthesis. If you see `[RESEARCH_LOG]` in the context, synthesize it into your thinking but DO NOT include the raw list in your final `response` field."
        f"\n12. EFFICIENCY: Avoid making multiple sequential tool calls if a single call or no tool call can answer the user's question. Tool calls add latency — only use them when necessary for real-time data, file content retrieval, or specific citations. For straightforward knowledge questions, general advice, or topics you can answer from your training, provide the answer directly without invoking any tools. If one tool call provides sufficient information, do NOT make additional calls — synthesize and respond."
        f"\n13. EFFICIENCY & TIMEOUTS: Respect the user's time. Avoid excessive or repetitive internal reasoning (overthinking) that leads to long wait times or timeouts. If the answer is straightforward, provide it quickly. Only use deep reasoning or multiple tool calls when the complexity strictly requires it."
    )
    
    # Add optimization hint if previous interruptions occurred
    optimization_hint = ""
    if interruption_context and interruption_context.get('count', 0) > 0:
        count = interruption_context['count']
        reason = interruption_context.get('reason', 'excessive tool calls or tokens')
        optimization_hint = (
            f"\n\n### OPTIMIZATION REMINDER (Based on Previous Interruptions) ###"
            f"\nWARNING: This conversation was interrupted {count} time(s) due to {reason}."
            f"\nTo prevent further interruptions and timeouts:"
            f"\n- Be MORE conservative with tool calls — aim for 3-5 maximum per iteration."
            f"\n- Prefer simpler, direct approaches over complex multi-step tool chains."
            f"\n- If you have partial results from previous attempts, use them rather than re-fetching."
            f"\n- NO OVERTHINKING: Synthesize information quickly — avoid repetitive reasoning loops."
            f"\n- Respect the user's time. Provide the best answer you can with minimal tool usage."
        )
    
    mode_augmentation = ""
    if intent == 'coding':
        mode_augmentation = "\n\n### CODING MODE ACTIVE ###\n- Prioritize technical precision and robust error handling.\n- You have an `execute_python_code` tool. You MUST use this tool to verify, test, or run scripts if the user asks you to execute code. Do not claim you lack a sandbox.\n- Provide complete, copyable code snippets."
    elif intent == 'file_manipulation':
        mode_augmentation = "\n\n### FILE MANIPULATION MODE ACTIVE ###\n- You have direct access to workspace files via tools.\n- Verify file state before and after any modification."
    elif intent == 'research':
        mode_augmentation = "\n\n### DEEP RESEARCH MODE ACTIVE ###\n- Synthesize information from multiple distinct sources.\n- Identify contradictions in sources and present a balanced view.\n- Heavily utilize your `web_search` and `read_url` tools."
    elif intent in ['search', 'chat']:
        mode_augmentation = "\n\n### GENERAL ASSISTANT MODE ###\n- You have access to `web_search`, `image_search`, `video_search`, and `suggest_workflow`. Use the most appropriate tool to answer the user's query dynamically."
    elif intent == 'workflow':
        mode_augmentation = "\n\n### WORKFLOW ASSISTANT MODE ###\n- You have a `suggest_workflow` tool. Prioritize invoking it to help the user find or build automation sequences."
    elif intent == 'image':
        mode_augmentation = "\n\n### IMAGE GENERATION/SEARCH MODE ###\n- The user wants visuals. Prioritize invoking the `image_search` tool."
    elif intent == 'video':
        mode_augmentation = "\n\n### VIDEO SEARCH MODE ###\n- The user wants video content. Prioritize invoking the `video_search` tool."
    
    format_instr = get_format_instructions(provider, model)
    
    import chat.tools as shared_tools
    tool_list_str = "\n\n### AVAILABLE TOOLS ###\nYou have direct access to the following tools. Use them when appropriate:\n"
    for t in shared_tools.AVAILABLE_TOOLS:
        fn = t.get("function", {})
        tool_list_str += f"- `{fn.get('name')}`: {fn.get('description')}\n"
    
    return f"{base}{context_block}{directives}{tool_list_str}{mode_augmentation}{format_instr}{optimization_hint}"


# ==================== Dynamic LLM Execution ====================
async def execute_llm(
    provider: str,
    model: str,
    prompt: str,
    system_message: str,
    user_id: int,
    temperature: float = 0.7,
    max_tokens: int = 4096,
    tools: list = None,
    history: list = None,
    response_format: str = "text",
    attachments: list = None,
    stream: bool = False,
) -> Any:
    """
    Dynamically route to the correct LLM handler via the node registry.
    Returns {content, usage}
    """
    from nodes.handlers.registry import get_registry
    from compiler.schemas import ExecutionContext
    from credentials.models import Credential
    from asgiref.sync import sync_to_async

    registry = get_registry()
    node_type = PROVIDER_NODE_MAP.get(provider, provider)

    if not registry.has_handler(node_type):
        return {
            "content": f"Error: Provider '{provider}' is not available.",
            "usage": {},
        }

    handler = registry.get_handler(node_type)

    # Fetch active credential
    credential_id = None
    if provider != 'ollama':
        def get_cred():
            cred_type = provider
            if provider == 'gemini':
                return Credential.objects.filter(
                    user_id=user_id,
                    credential_type__slug__in=['gemini-api', 'google-oauth2'],
                    is_active=True,
                    is_verified=True
                ).first()
            elif provider == 'perplexity':
                cred_type = 'perplexity-api'
            elif provider == 'xai':
                cred_type = 'xai-api'
            
            return Credential.objects.filter(
                user_id=user_id,
                credential_type__slug=cred_type,
                is_active=True,
                is_verified=True
            ).first()
            
        active_cred = await sync_to_async(get_cred)()
        if active_cred:
            credential_id = str(active_cred.id)
        else:
            return {"content": f"Error: No verified credentials for {provider}", "usage": {}}

    context = ExecutionContext(
        execution_id=uuid4(),
        user_id=user_id,
        workflow_id=0,
    )

    config = {
        "prompt": prompt,
        "model": model,
        "system_message": system_message,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "credential": credential_id,
        "history": history or [],
        "response_format": response_format,
        "attachments": attachments or [],
    }
    
    if tools:
        config["tools"] = tools

    if stream:
        return handler.stream_execute({}, config, context)

    try:
        result = await handler.execute({}, config, context)
        if result.success:
            data = result.get_data() if hasattr(result, 'get_data') else (result.items[0].json if result.items else {})
            content = data.get("content", "")
            usage = data.get("usage", {})
            ret = {"content": content, "usage": usage}
            if "tool_calls" in data:
                ret["tool_calls"] = data["tool_calls"]
            if "media_url" in data:
                ret["media_url"] = data["media_url"]
            if "thinking" in data:
                ret["thinking"] = data["thinking"]
            return ret
        else:
            return {"content": f"LLM Error: {result.error}", "usage": {}}
    except Exception as e:
        logger.exception(f"LLM execution failed: {e}")
        return {"content": f"Internal Error: {str(e)}", "usage": {}}


async def ensure_final_response_payload(
    *,
    raw_content: str,
    full_prompt: str,
    provider: str,
    model: str,
    system_message: str,
    user_id: int,
    response_format: str,
    tool_context_text: str = "",
    thinking: str = "",
) -> tuple[str, int]:
    """
    Guarantee that we persist a real final payload, not intermediate tool/action prose.
    Returns (normalized_raw_content, extra_tokens_used).
    """
    if has_structured_final_response(raw_content):
        return raw_content, 0

    cleaned_prior = strip_tool_calls(raw_content or "").strip()
    clipped_prior = cleaned_prior[:3000]
    clipped_tools = (tool_context_text or "")[:12000]

    repair_prompt = (
        f"{full_prompt}\n\n"
        "Your previous output was not a valid final payload in the required format.\n\n"
        f"Previous output:\n{clipped_prior or '[empty]'}\n\n"
        f"Tool evidence (if any):\n{clipped_tools or '[none]'}\n\n"
        "Return ONLY a final JSON object with a non-empty 'response' field. "
        "Do NOT describe future actions like searching or checking."
    )

    repair_result = await execute_llm(
        provider=provider,
        model=model,
        prompt=repair_prompt,
        system_message=system_message,
        user_id=user_id,
        max_tokens=16384,
        response_format=response_format,
    )
    extra_tokens = repair_result.get("usage", {}).get("total_tokens", 0)
    repaired_content = repair_result.get("content") or ""
    # Capture thinking from repair pass too
    if repair_result.get("thinking"):
        thinking = (thinking or "") + "\n\n" + repair_result["thinking"]

    if has_structured_final_response(repaired_content):
        return repaired_content, extra_tokens

    # Deterministic final fallback: use whatever cleaned content we have.
    # Only show the generic error if content is truly empty or is just unresolved tool syntax.
    fallback_text = cleaned_prior
    if not fallback_text or has_unresolved_tool_syntax(fallback_text):
        fallback_text = (
            "I could not generate a stable final answer format for this turn. Please retry.\n\n"
        )
        # If we have tool context or thinking, include it so the user sees progress
        if tool_context_text:
            fallback_text += f"**Research gathered so far:**\n{tool_context_text[:3000]}\n\n"
        if thinking:
            fallback_text += f"**Model thinking:**\n{thinking[:8000]}"
    elif looks_like_intermediate_action_text(fallback_text) and len(fallback_text) < 200:
        # Only discard short intermediate text like "I'll search for that"
        # Long responses that happen to mention "let me" are real answers
        fallback_text = (
            "I could not generate a stable final answer format for this turn. Please retry.\n\n"
        )
        if thinking:
            fallback_text += f"**Model thinking:**\n{thinking[:8000]}"

    forced_payload = (
        "<json_response>\n"
        + json.dumps({"response": fallback_text, "follow_ups": [], "thinking": thinking or ""}, ensure_ascii=False)
        + "\n</json_response>"
    )
    return forced_payload, extra_tokens


# ==================== Assistant Summarization ====================
# (Removed `generate_assistant_summary` to avoid Gemini API quota issues.
#  Summarization is now handled via simple text truncation inline.)


# ==================== Workflow Suggestion ====================
async def suggest_workflow(user_id: int, intent_description: str) -> dict | None:
    """
    Find the best matching workflow for the user's intent.
    Returns { workflow_id, name, description } or None.
    """
    from orchestrator.models import Workflow
    from asgiref.sync import sync_to_async

    try:
        workflows = await sync_to_async(list)(
            Workflow.objects.filter(user_id=user_id).values('id', 'name', 'description')[:20]
        )

        if not workflows:
            return None

        # Simple keyword matching for now; can be upgraded to LLM-based matching later
        intent_lower = intent_description.lower()
        best_match = None
        best_score = 0

        for wf in workflows:
            name_lower = (wf.get('name', '') or '').lower()
            desc_lower = (wf.get('description', '') or '').lower()
            
            # Score = number of intent words found in workflow name/description
            score = sum(1 for word in intent_lower.split() if word in name_lower or word in desc_lower)
            
            if score > best_score:
                best_score = score
                best_match = wf

        if best_match and best_score >= 2:
            return {
                "workflow_id": best_match['id'],
                "name": best_match['name'],
                "description": best_match.get('description', ''),
            }

        return None
    except Exception as e:
        logger.error(f"Workflow suggestion failed: {e}")
        return None


# ==================== ViewSet ====================
class ChatSessionViewSet(viewsets.ModelViewSet):
    """ViewSet for managing standalone chat sessions."""
    serializer_class = ChatSessionSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        return ChatSession.objects.filter(user=self.request.user)

    def perform_create(self, serializer):
        # We don't strictly need to preemptively create the KB here since it lazy-loads,
        # but the session itself is created.
        session = serializer.save(user=self.request.user)

    def perform_destroy(self, instance):
        """
        Override the default destroy to eagerly clean up the associated 
        Session Knowledge Base (FAISS memory) and orphaned RAG documents.
        """
        session_id = str(instance.id)
        
        # 1. Gather all attachments with inference documents
        try:
            attachments = ChatAttachment.objects.filter(session=instance)
            doc_ids_to_delete = []
            for att in attachments:
                if att.inference_document_id:
                    doc_ids_to_delete.append(att.inference_document_id)
            
            # 2. Delete SQL inference records (cascades chunks and files)
            if doc_ids_to_delete:
                from inference.models import Document as InfModel
                InfModel.objects.filter(id__in=doc_ids_to_delete).delete()
                
        except Exception as e:
            logger.error(f"Failed to cleanup inference sql documents for session {session_id}: {e}")
            
        # 3. Clear the FAISS vector KB from memory
        try:
            from inference.engine import get_session_kb_manager
            get_session_kb_manager().clear_session_kb(session_id)
        except Exception as e:
            logger.error(f"Failed to clear Session KB from memory for {session_id}: {e}")
            
        # 4. Finally, delete the session itself
        instance.delete()


# ==================== Main Send Message ====================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def send_message(request, session_id: str):
    """
    Send a message to a standalone chat session and get an AI response.
    
    Request body:
        content (str): The user's message text (clean, no slash commands needed)
        intent (str, optional): Explicit intent from the frontend.
            One of: chat, search, image, video, workflow.
            If omitted, falls back to classify_intent() for backward compatibility.
    
    The frontend buttons (Search, Research, Visualize, Motion) should send
    the intent as a parameter rather than prepending a slash command to the content.
    """
    try:
        session_uuid = UUID(session_id)
        session = await ChatSession.objects.filter(id=session_uuid, user=request.user).afirst()
    except ValueError:
        return Response({'error': 'Invalid session ID format'}, status=400)

    if not session:
        return Response({'error': 'Chat session not found'}, status=404)

    content = request.data.get('content', '')
    if not content:
        return Response({'error': 'Content is required'}, status=400)

    # ---- Intent Resolution ----
    # Priority: explicit intent param > slash command parsing > heuristic detection
    explicit_intent = request.data.get('intent', '').strip().lower()
    
    if explicit_intent and explicit_intent in ('chat', 'search', 'research', 'image','video', 'workflow', 'coding', 'file_manipulation'):
        intent = explicit_intent
        clean_content = content.strip()
    else:
        # Fallback: parse slash commands from content for backward compatibility
        intent, clean_content = classify_intent(content)

    # Save the user's message (store clean content, not slash-prefixed)
    user_msg = await ChatMessage.objects.acreate(
        session=session,
        role='user',
        content=clean_content,
        message_type='chat',
    )

    # ---- Handle /video (Coming Soon) ----
    if intent == 'video':
        ai_msg = await ChatMessage.objects.acreate(
            session=session,
            role='assistant',
            content="🎬 **Video generation is coming soon!**\n\nThis feature is currently under development. Stay tuned for updates!",
            message_type='video',
            metadata={'intent': 'video'},
        )
        return Response({
            'user_message': await serialize_message(user_msg),
            'ai_response': await serialize_message(ai_msg),
        })

    # ---- Handle /image ----
    if intent == 'image':
        # TODO: Route to DALL-E or other T2I service
        ai_msg = await ChatMessage.objects.acreate(
            session=session,
            role='assistant',
            content=f"🎨 **Image generation request received:**\n\n*\"{clean_content}\"*\n\nImage generation via DALL-E/Stable Diffusion is being integrated. Coming soon!",
            message_type='image',
            metadata={'intent': 'image', 'prompt': clean_content},
        )
        return Response({
            'user_message': await serialize_message(user_msg),
            'ai_response': await serialize_message(ai_msg),
        })

    provider, model = resolve_provider_model(request.data, session)
    await sync_session_model_overrides(request.data, session, provider, model)
    current_time_str = current_time_string()
    
    # Check for previous interruptions to add optimization hints
    interruption_context = await get_interruption_context(session)

    system_message = build_augmented_system_message(session, current_time_str, intent, provider, model, interruption_context)

    _, history_list, history_prompt = await prepare_history_context(
        session=session,
        excluded_message_id=user_msg.id,
        supports_docs=False,
        list_token_budget=0,
        prompt_token_budget=MAX_CONTEXT_TOKENS,
    )

    metadata = {
        'intent': intent,
        'model': model,
        'provider': provider,
    }

    import chat.tools as shared_tools
    tools_payload = shared_tools.AVAILABLE_TOOLS

    total_tokens = 0  # accumulate tokens across ALL LLM calls in this request
    thinking = ""
    accumulated_tool_context = []

    # ---- Eager Tool Execution for Explicit Intents ----
    # This saves 1 LLM roundtrip (~5-10s) when the user uses a button or slash command.
    eager_results = []
    if intent in ['search', 'research', 'workflow']:
        if intent == 'search':
            search_data = await run_eager_search_intent(clean_content, request.user.id, shared_tools)
            metadata['search_query'] = search_data["search_query"]
            if search_data["sources"]:
                metadata['sources'] = search_data["sources"]
            if search_data["eager_text"]:
                eager_results.append(f"[Eager Tool: web_search executed]\nResult: {search_data['eager_text']}")
            else:
                eager_results.append(f"[Eager Tool: web_search executed]\nResult: {search_data['raw_result']}")
            
            # --- Proactive Media Search ---
            img_task = perform_image_search(clean_content)
            vid_task = perform_video_search(clean_content)
            img_res, vid_res = await asyncio.gather(img_task, vid_task)
            
            if img_res.get("images"):
                metadata['images'] = img_res["images"]
            if vid_res.get("videos"):
                metadata['videos'] = vid_res["videos"]
        elif intent == 'research':
            # Deep Research Loop
            
            # 1. Ask LLM to define queries and count based on location/time
            plan_prompt = f"User asked for deep research: '{clean_content}'.\n\nCurrent System Datetime is {current_time_str}.\n\nGenerate a JSON object with two keys:\n1. 'queries': A list of 2 to 4 distinct search queries to gather comprehensive information. The queries MUST incorporate relevant date/time contexts to ensure the latest information is retrieved.\n2. 'total_links': An integer between 15 and 50 representing the total number of web pages to deeply read across these queries.\n\nRespond ONLY with valid JSON."
            plan_sys = "You are an expert research planner. Output only valid JSON."
            plan_res = await execute_llm(provider, model, plan_prompt, plan_sys, request.user.id)
            total_tokens += plan_res.get("usage", {}).get("total_tokens", 0)
            if plan_res.get('thinking'):
                thinking += plan_res['thinking'] + "\n\n"
            
            plan_json = parse_first_json_object(plan_res.get('content', ''))
            queries = normalize_research_queries(plan_json, clean_content)
            total_links = normalize_total_links(plan_json.get('total_links', 15))
                
            all_urls = []
            all_sources = []
            # Gather search results dynamically across queries
            for q in queries:
                res = await perform_web_search(q, max_results=10)
                for src in res.get("sources", []):
                    u = src.get("url")
                    if u and u not in all_urls:
                        all_urls.append(u)
                        all_sources.append(src)
                        
            all_urls = all_urls[:total_links]
            all_sources = all_sources[:total_links]
            
            scraped_texts, valid_sources = await scrape_sources(all_sources)
                    
            metadata['search_queries'] = queries
            metadata['sources'] = valid_sources
            
            combined = "\n\n".join(scraped_texts)
            # Hard limit to prevent blowing up the LLM token budget (~60,000 characters)
            combined = combined[:60000] 
            
            # --- Integrated Image/Video Search ---
            img_task = perform_image_search(clean_content)
            vid_task = perform_video_search(clean_content)
            img_res, vid_res = await asyncio.gather(img_task, vid_task)
            
            if img_res.get("images"):
                metadata['images'] = img_res["images"]
            if vid_res.get("videos"):
                metadata['videos'] = vid_res["videos"]
                
            eager_results.append(f"[Deep Research Performed]\nQueries Decided: {queries}\nTotal Links Analyzed: {len(valid_sources)}\nImages: {len(metadata.get('images', []))}\nVideos: {len(metadata.get('videos', []))}\n\nExtracted Content for Synthesis:\n{combined}\n\nReview this deeply researched data, understanding you must analyze the dates/times and provide an exceptionally robust response.")
            
        elif intent == 'workflow':
            workflow_data = await run_workflow_suggestion_intent(clean_content, request.user.id, shared_tools)
            if workflow_data["workflow_id"]:
                metadata['workflow_id'] = workflow_data["workflow_id"]
                metadata['workflow_name'] = workflow_data["workflow_name"]
            eager_results.append(f"[Eager Tool: suggest_workflow executed]\nResult: {workflow_data['raw_result']}")
            
        # These intents add specific behavioral instructions instead of terminating early
        if intent == 'coding':
            system_message += "\n\n### CODING MODE ACTIVE ###\n- Prioritize technical precision, clean code, and robust error handling.\n- Use code blocks for all snippets.\n- If the user asks for a project fix, use tools to read the code first."
            
        if intent == 'file_manipulation':
            system_message += "\n\n### FILE MANIPULATION MODE ACTIVE ###\n- You are specialized in reading, writing, and organizing workspace files.\n- Use the provided file system tools to fulfill requests accurately.\n- Always verify file existence before modification."

    # Determine response format (use json_object if supported by model/handler)
    response_format = "json_object" if provider in ['openai', 'gemini', 'openrouter'] else "text"

    # Build the decision prompt
    if history_prompt:
        full_prompt = f"[CONVERSATION HISTORY]\n{history_prompt}\n[END HISTORY]\n\nUser: {clean_content}"
    else:
        full_prompt = clean_content

    # Inject message reference context silently to the LLM
    reference_data = request.data.get('reference')
    if reference_data and isinstance(reference_data, dict):
        ref_msg_id = reference_data.get('message_id')
        if ref_msg_id:
            full_prompt += f"\n\n[SYSTEM INSTRUCTION: The user's query specifically refers to a portion of Message ID {ref_msg_id}. Please prioritize this context when answering.]"

    # If we have eager results, we inject them and treat this as the 'final' call
    interrupted = False  # Track if loop was interrupted due to timeout/limits
    if eager_results:
        combined_eager = "\n\n".join(eager_results)
        prompt_with_context = f"{full_prompt}\n\nAdditional context from tools:\n{combined_eager}\n\nPlease provide your final answer based on these results in the requested JSON format."
        
        llm_result = await execute_llm(
            provider=provider,
            model=model,
            prompt=prompt_with_context,
            system_message=system_message,
            user_id=request.user.id,
            max_tokens=16384,
            history=history_list,
            response_format=response_format,
        )
        raw_content = llm_result.get("content") or ""
        total_tokens += llm_result.get("usage", {}).get("total_tokens", 0)
        if llm_result.get("media_url"):
            metadata['media_url'] = llm_result.get("media_url")
    else:
        # ============================================================
        # AGENTIC TOOL LOOP — LangGraph StateGraph
        # ============================================================
        from chat.graph import run_agent_loop

        graph_result = await run_agent_loop(
            full_prompt=full_prompt,
            metadata=metadata,
            provider=provider,
            model=model,
            system_message=system_message,
            user_id=request.user.id,
            response_format=response_format,
            clean_content=clean_content,
            intent=intent,
            history_list=history_list,
            attachments=[],
            stream_callback=None,
            max_iterations=resolve_agent_iteration_limit(intent),
        )
        raw_content = graph_result["raw_content"]
        metadata = graph_result["metadata"]
        tool_trace = graph_result["tool_trace"]
        thinking = graph_result["thinking"]
        total_tokens += graph_result["total_tokens"]
        accumulated_tool_context = graph_result["accumulated_tool_context"]
        interrupted = graph_result["interrupted"]

        if tool_trace:
            metadata['tool_trace'] = tool_trace

    tool_context_text = "\n\n".join(accumulated_tool_context) if accumulated_tool_context else "\n\n".join(eager_results)
    raw_content, normalize_tokens = await ensure_final_response_payload(
        raw_content=raw_content,
        full_prompt=full_prompt,
        provider=provider,
        model=model,
        system_message=system_message,
        user_id=request.user.id,
        response_format=response_format,
        tool_context_text=tool_context_text,
        thinking=thinking,
    )
    total_tokens += normalize_tokens

    # ---- Normalize final LLM output into canonical payload ----
    payload = normalize_llm_payload(raw_content, provider, model, tool_context_text, metadata, llm_result=llm_result)

    # Use human-renderable response as content and persist structured data in metadata
    response_text = payload.get('response', '')

    # Merge payload metadata into existing metadata
    payload_meta = payload.copy()
    payload_meta.pop('response', None)
    inner_meta = payload_meta.pop('metadata', {}) if isinstance(payload_meta.get('metadata', {}), dict) else {}
    # Update base metadata (keeps existing search/workflow keys)
    metadata.update(inner_meta)

    # Copy structured fields into metadata
    for k in ('follow_ups', 'thinking', 'sources', 'images', 'videos', 'tool_trace'):
        if payload_meta.get(k) is not None:
            metadata[k] = payload_meta.get(k)

    # Add interruption metadata if process was interrupted
    if interrupted:
        metadata['interrupted'] = True
        metadata['partial_results'] = "\n\n".join(accumulated_tool_context)[:3000] if accumulated_tool_context else ""
        metadata['iterations_completed'] = iteration + 1
        metadata['max_iterations'] = actual_max_iterations

    # Update session token count
    session.total_tokens_used += total_tokens
    await session.asave(update_fields=['total_tokens_used'])

    # Ensure tokens in metadata
    metadata['tokens'] = total_tokens

    # Save AI response
    ai_msg = await ChatMessage.objects.acreate(
        session=session,
        role='assistant',
        content=response_text,
        message_type='workflow_suggestion' if 'workflow_id' in metadata else ('search' if 'search_query' in metadata else 'chat'),
        metadata=metadata,
    )

    return Response({
        'user_message': await serialize_message(user_msg),
        'ai_response': await serialize_message(ai_msg),
    })


# ==================== SSE Streaming Send Message ====================
from django.views.decorators.csrf import csrf_exempt

@csrf_exempt
async def send_message_stream(request, session_id: str):
    """
    SSE streaming version of send_message.
    Streams real-time progress events (tool calls, sources, status) to the frontend.

    Event types:
        - status:         { "phase": "...", "message": "..." }
        - tool_call:      { "tool": "...", "args": {...}, "iteration": N }
        - sources_update: { "sources": [...] }
        - done:           { "user_message": {...}, "ai_response": {...} }
        - error:          { "message": "..." }
    """
    import json as _json
    from django.http import StreamingHttpResponse

    # --- Manual JWT auth (DRF decorators don't work with StreamingHttpResponse) ---
    user = request.user
    if not user or not user.is_authenticated:
        # Try JWT from Authorization header
        auth_header = request.META.get('HTTP_AUTHORIZATION', '')
        if auth_header.startswith('Bearer '):
            from rest_framework_simplejwt.tokens import AccessToken
            from django.contrib.auth import get_user_model
            try:
                token = AccessToken(auth_header.split(' ')[1])
                User = get_user_model()
                user = await User.objects.aget(id=token['user_id'])
            except Exception:
                user = None

    if not user or (hasattr(user, 'is_authenticated') and not user.is_authenticated):
        async def auth_error():
            yield f"data: {json.dumps({'type': 'error', 'message': 'Authentication required'})}\n\n"
        return StreamingHttpResponse(auth_error(), content_type='text/event-stream')

    # Bind the authenticated user so event_stream can use it
    request.user = user

    # --- Parse request body (plain Django view, no request.data) ---
    try:
        req_data = _json.loads(request.body)
    except Exception:
        req_data = {}

    async def event_stream():
        try:
            session_uuid = UUID(session_id)
            session = await ChatSession.objects.filter(id=session_uuid, user=request.user).afirst()
        except ValueError:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Invalid session ID format'})}\n\n"
            return

        if not session:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Chat session not found'})}\n\n"
            return

        content = req_data.get('content', '')
        if not content:
            yield f"data: {json.dumps({'type': 'error', 'message': 'Content is required'})}\n\n"
            return
            
        from nodes.models import AIModel
            
        reference_data = req_data.get('reference')
        
        provider, model = resolve_provider_model(req_data, session)

        # ---- Intent Resolution (No Locking) ----
        explicit_intent = req_data.get('intent', '').strip().lower()
        
        if explicit_intent and explicit_intent in ('chat', 'search', 'research', 'image', 'video', 'workflow', 'coding', 'file_manipulation'):
            intent = explicit_intent
            clean_content = content.strip()
        else:
            intent, clean_content = classify_intent(content)

        yield f"data: {json.dumps({'type': 'status', 'phase': 'planning', 'message': 'Initializing agent core...'})}\n\n"
        yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': 'Analyzing user request and preparing reasoning engine...'})}\n\n"

        # Save the user's message
        user_msg = await ChatMessage.objects.acreate(
            session=session, role='user', content=clean_content, message_type='chat',
        )

        yield f"data: {json.dumps({'type': 'status', 'phase': 'thinking', 'message': 'Processing your message...', 'user_message_id': user_msg.id})}\n\n"
        yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': f'Intent resolved as: {intent}. Bootstrapping mental model...'})}\n\n"

        # ---- Handle /video ----
        if intent == 'video':
            ai_model_obj = await AIModel.objects.filter(value=model, is_active=True).afirst()
            
            if not ai_model_obj or not ai_model_obj.supports_video_generation:
                ai_msg = await ChatMessage.objects.acreate(
                    session=session, role='assistant',
                    content=f"❌ **Model Incompatible**\n\nThe current model (`{model}`) does not support video generation. Please switch to a compatible model (e.g., Grok Imagine Video) to use this feature.",
                    message_type='chat',
                )
                yield f"data: {json.dumps({'type': 'done', 'user_message': await serialize_message(user_msg), 'ai_response': await serialize_message(ai_msg)})}\n\n"
                return

            # If compatible, we'll let the standard LLM loop handle it (will be implemented in execute_llm)
            yield f"data: {json.dumps({'type': 'status', 'phase': 'motion_generating', 'message': 'Creating Video with Grok...'})}\n\n"
            pass 

        # ---- Handle /image ----
        if intent == 'image':
            ai_model_obj = await AIModel.objects.filter(value=model, is_active=True).afirst()

            if not ai_model_obj or not ai_model_obj.supports_image_generation:
                ai_msg = await ChatMessage.objects.acreate(
                    session=session, role='assistant',
                    content=f"❌ **Model Incompatible**\n\nThe current model (`{model}`) does not support image generation. Please switch to a compatible model (e.g., Grok Imagine Image) to use this feature.",
                    message_type='chat',
                )
                yield f"data: {json.dumps({'type': 'done', 'user_message': await serialize_message(user_msg), 'ai_response': await serialize_message(ai_msg)})}\n\n"
                return

            # If compatible, we'll let the standard LLM loop handle it
            yield f"data: {json.dumps({'type': 'status', 'phase': 'visualizing', 'message': 'Generating Image with Grok...'})}\n\n"
            pass

        # Check model capabilities for multimodal support
        ai_model_obj = await AIModel.objects.filter(value=model, is_active=True).afirst()
        supports_docs = ai_model_obj.supports_document_input if ai_model_obj else False

        history_messages, history_list, _history_prompt = await prepare_history_context(
            session=session,
            excluded_message_id=user_msg.id,
            supports_docs=supports_docs,
            list_token_budget=MAX_CONTEXT_TOKENS - 4000,
            prompt_token_budget=MAX_CONTEXT_TOKENS,
        )

        await sync_session_model_overrides(req_data, session, provider, model)
        c_time = current_time_string()
        
        # Check for previous interruptions to add optimization hints
        interruption_context = await get_interruption_context(session)

        system_message = build_augmented_system_message(session, c_time, intent, provider, model, interruption_context)
        response_format = "json_object" if provider in ['openai', 'gemini', 'openrouter'] else "text"

        meta = {'intent': intent, 'model': model, 'provider': provider}

        import chat.tools as shared_tools
        tools_payload = shared_tools.AVAILABLE_TOOLS
        total_tokens = 0
        thinking = ""
        raw_content = ""
        tool_trace = []
        accumulated_tool_context = []

        # ---- Eager Tool Execution ----
        eager_results = []
        if intent in ['search', 'research', 'workflow']:
            if intent == 'search':
                yield f"data: {json.dumps({'type': 'status', 'phase': 'searching', 'message': 'Searching the web...'})}\n\n"
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': 'web_search', 'args': {'query': clean_content}, 'iteration': 0})}\n\n"
                tool_trace.append({"tool": "web_search", "args": {"query": clean_content}, "iteration": 0})

                search_data = await run_eager_search_intent(clean_content, request.user.id, shared_tools)
                meta['search_query'] = search_data["search_query"]
                if search_data["sources"]:
                    meta['sources'] = search_data["sources"]
                    yield f"data: {json.dumps({'type': 'sources_update', 'sources': meta['sources']})}\n\n"
                if search_data["eager_text"]:
                    eager_results.append(f"[Eager Tool: web_search executed]\nResult: {search_data['eager_text']}")
                else:
                    eager_results.append(f"[Eager Tool: web_search executed]\nResult: {search_data['raw_result']}")
                # --- Proactive Media Search ---
                yield f"data: {json.dumps({'type': 'status', 'phase': 'searching', 'message': 'Finding images and videos...'})}\n\n"
                img_task = perform_image_search(clean_content)
                vid_task = perform_video_search(clean_content)
                img_res, vid_res = await asyncio.gather(img_task, vid_task)

                if img_res.get("images"):
                    meta['images'] = img_res["images"]
                    yield f"data: {json.dumps({'type': 'images_update', 'images': meta['images']})}\n\n"

                if vid_res.get("videos"):
                    meta['videos'] = vid_res["videos"]
                    yield f"data: {json.dumps({'type': 'videos_update', 'videos': meta['videos']})}\n\n"

            elif intent == 'research':
                
                # --- Step 1: Human-in-the-Loop Clarification Check (Pro Search) ---
                yield f"data: {json.dumps({'type': 'status', 'phase': 'thinking', 'message': 'Analyzing research query...'})}\n\n"
                yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': 'Analyzing user request for ambiguity and historical context...'})}\n\n"
                
                recent_context_messages = append_history_message(history_messages[-3:], user_msg, max_window=4)
                recent_hist = "\n".join([f"{m.role.upper()}: {m.content}" for m in recent_context_messages])
                clarify_prompt = (
                    f"Evaluate this user research request: '{clean_content}'.\n\n"
                    f"Recent Conversation History (for context):\n{recent_hist}\n\n"
                    f"If the request is highly specific and clear enough to conduct deep web research, OR if the user is clearly answering a previous clarifying question from the history, output exactly: {{\"needs_clarification\": false}}\n\n"
                    f"If the request is brand new and broad, ambiguous, or lacks context (e.g. 'Apple', 'React', 'What happened yesterday?'), you must ask ONE specific clarifying question (e.g., 'Are you referring to the company Apple or the fruit?'). Output exactly: {{\"needs_clarification\": true, \"question\": \"<your question here>\"}}\n\n"
                    f"Respond ONLY with valid JSON."
                )
                clarify_content = ""
                clarify_stream = await execute_llm(provider, model, clarify_prompt, "You are a research planner. Output only valid JSON.", request.user.id, stream=True)
                async for chunk in clarify_stream:
                    if chunk["type"] == "thinking":
                        thought = chunk["content"]
                        thinking += thought
                        yield f"data: {json.dumps({'type': 'thinking_chunk', 'content': thought})}\n\n"
                    elif chunk["type"] == "content":
                        clarify_content += chunk["content"]
                    elif chunk["type"] == "metadata":
                        total_tokens += chunk.get("usage", {}).get("total_tokens", 0)
                    elif chunk["type"] == "error":
                        logger.error(f"[Deep Research] Clarify stream error: {chunk.get('message')}")

                clarify_json = parse_first_json_object(clarify_content)
                
                if clarify_json.get("needs_clarification") and clarify_json.get("question"):
                    # Halt research and ask the user
                    ai_msg = await ChatMessage.objects.acreate(
                        session=session, role='assistant',
                        content=f"🤔 **Clarification needed before deep research:**\n\n{clarify_json.get('question')}",
                        message_type='chat', metadata={'intent': 'research_clarification', 'thinking': thinking},
                    )
                    yield f"data: {json.dumps({'type': 'done', 'user_message': await serialize_message(user_msg), 'ai_response': await serialize_message(ai_msg)})}\n\n"
                    return

                # --- Step 2: Proceed with Research Plan ---
                yield f"data: {json.dumps({'type': 'status', 'phase': 'planning', 'message': 'Planning research strategy...'})}\n\n"
                yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': 'Designing optimized search queries and defining target link depth...'})}\n\n"

                plan_prompt = f"User asked for deep research: '{clean_content}'.\n\nCurrent System Datetime is {c_time}.\n\nGenerate a JSON object with two keys:\n1. 'queries': A list of 2 to 4 distinct search queries.\n2. 'total_links': An integer between 15 and 50.\n\nRespond ONLY with valid JSON."
                plan_sys = "You are an expert research planner. Output only valid JSON."
                plan_content = ""
                plan_stream = await execute_llm(provider, model, plan_prompt, plan_sys, request.user.id, stream=True)
                async for chunk in plan_stream:
                    if chunk["type"] == "thinking":
                        thought = chunk["content"]
                        thinking += thought
                        yield f"data: {json.dumps({'type': 'thinking_chunk', 'content': thought})}\n\n"
                    elif chunk["type"] == "content":
                        plan_content += chunk["content"]
                    elif chunk["type"] == "metadata":
                        total_tokens += chunk.get("usage", {}).get("total_tokens", 0)
                    elif chunk["type"] == "error":
                        logger.error(f"[Deep Research] Plan stream error: {chunk.get('message')}")

                plan_json = parse_first_json_object(plan_content)
                if not plan_json:
                    # Fallback if streaming failed to produce valid JSON
                    queries = [clean_content]
                    total_links_plan = 15
                else:

                    queries = normalize_research_queries(plan_json, clean_content)
                    total_links_plan = normalize_total_links(plan_json.get('total_links', 15))

                yield f"data: {json.dumps({'type': 'status', 'phase': 'searching', 'message': f'Searching across {len(queries)} queries...'})}\n\n"

                all_urls, all_sources = [], []
                for q_idx, q in enumerate(queries):
                    yield f"data: {json.dumps({'type': 'tool_call', 'tool': 'web_search', 'args': {'query': q}, 'iteration': q_idx + 1})}\n\n"
                    tool_trace.append({"tool": "web_search", "args": {"query": q}, "iteration": q_idx + 1})
                    res = await perform_web_search(q, max_results=10)
                    for src in res.get("sources", []):
                        u = src.get("url")
                        if u and u not in all_urls:
                            all_urls.append(u)
                            all_sources.append(src)
                    yield f"data: {json.dumps({'type': 'sources_update', 'sources': all_sources})}\n\n"

                all_urls = all_urls[:total_links_plan]
                all_sources = all_sources[:total_links_plan]
                yield f"data: {json.dumps({'type': 'status', 'phase': 'reading', 'message': f'Reading {len(all_urls)} sources...'})}\n\n"

                scraped_texts, valid_sources = await scrape_sources(all_sources)

                meta['search_queries'] = queries
                meta['sources'] = valid_sources
                yield f"data: {json.dumps({'type': 'sources_update', 'sources': valid_sources})}\n\n"
                
                # --- Integrated Image/Video Search ---
                yield f"data: {json.dumps({'type': 'status', 'phase': 'searching', 'message': 'Gathering visuals...'})}\n\n"
                img_task = perform_image_search(clean_content)
                vid_task = perform_video_search(clean_content)
                img_res, vid_res = await asyncio.gather(img_task, vid_task)

                if img_res.get("images"):
                    meta['images'] = img_res["images"]
                    yield f"data: {json.dumps({'type': 'images_update', 'images': meta['images']})}\n\n"
                
                if vid_res.get("videos"):
                    meta['videos'] = vid_res["videos"]
                    yield f"data: {json.dumps({'type': 'videos_update', 'videos': meta['videos']})}\n\n"

                combined = "\n\n".join(scraped_texts)[:60000]
                eager_results.append(
                    f"[Deep Research Performed]\nQueries: {queries}\nLinks Analyzed: {len(valid_sources)}\n"
                    f"Images Gathered: {len(meta.get('images', []))}\nVideos Gathered: {len(meta.get('videos', []))}\n\n"
                    f"Extracted Content:\n{combined}\n\nProvide an exceptionally robust response."
                )
                yield f"data: {json.dumps({'type': 'status', 'phase': 'analyzing', 'message': f'Analyzed {len(valid_sources)} sources. Synthesizing...'})}\n\n"

            elif intent == 'workflow':
                yield f"data: {json.dumps({'type': 'tool_call', 'tool': 'suggest_workflow', 'args': {'intent': clean_content}, 'iteration': 0})}\n\n"
                tool_trace.append({"tool": "suggest_workflow", "args": {"intent": clean_content}, "iteration": 0})
                workflow_data = await run_workflow_suggestion_intent(clean_content, request.user.id, shared_tools)
                if workflow_data["workflow_id"]:
                    meta['workflow_id'] = workflow_data["workflow_id"]
                    meta['workflow_name'] = workflow_data["workflow_name"]
                eager_results.append(f"[Eager Tool: suggest_workflow executed]\nResult: {workflow_data['raw_result']}")

        # RAG is now fully tool-driven: the LLM calls list_knowledge_bases / knowledge_base_search
        # as needed rather than receiving injected context on every message.
        full_prompt = clean_content
        if reference_data and isinstance(reference_data, dict):
            ref_msg_id = reference_data.get('message_id')
            full_prompt = f"[SYSTEM INSTRUCTION: The user's query specifically refers to a portion of Message ID {ref_msg_id}. Please prioritize this context when answering.]\n\n{clean_content}"

        # ---- Generate final response ----
        llm_result = None
        interrupted = False  # Track if loop was interrupted due to timeout/limits
        if eager_results:
            yield f"data: {json.dumps({'type': 'status', 'phase': 'generating', 'message': 'Generating response...'})}\n\n"
            yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': 'Synthesizing all research findings and tool outputs into a final response...'})}\n\n"
            combined_eager = "\n\n".join(eager_results)
            prompt_ctx = f"{full_prompt}\n\nAdditional context from tools:\n{combined_eager}\n\nPlease provide your final answer in the requested JSON format."
            # Check model capabilities for multimodal support
            from nodes.models import AIModel
            ai_model_obj = await AIModel.objects.filter(value=model, is_active=True).afirst()
            supports_docs = ai_model_obj.supports_document_input if ai_model_obj else False

            # Collect attachments if model supports them
            active_attachments = []
            if supports_docs:
                att_ids = []
                # Only pull attachments from the last 5 turns to prevent context bloat
                for m in history_messages[-5:]:
                    if m.metadata and m.metadata.get('attachment_id'):
                        try:
                            att_ids.append(UUID(m.metadata['attachment_id']))
                        except: pass
                if att_ids:
                    # Filter for unique IDs and fetch
                    att_ids = list(set(att_ids))
                    active_attachments = await sync_to_async(list)(
                        ChatAttachment.objects.filter(id__in=att_ids).exclude(is_large_file=True)
                    )

            # Non-agentic: Standard LLM call with streaming
            
            try:
                stream_gen = await execute_llm(
                    provider=provider, model=model, prompt=prompt_ctx, 
                    system_message=system_message, user_id=request.user.id, history=history_list,
                    response_format=response_format,
                    attachments=active_attachments,
                    stream=True
                )
            except Exception as e:
                logger.exception(f"[Deep Research] Failed to start streaming LLM call: {e}")
                stream_gen = None
                raw_content = f"Error: Failed to start LLM synthesis: {str(e)}"
            
            if stream_gen is not None:
                try:
                    async with asyncio.timeout(LLM_STREAM_TIMEOUT):
                        async for chunk in stream_gen:
                            if chunk["type"] == "content":
                                content = chunk["content"]
                                raw_content += content
                                # yield f"data: {json.dumps({'type': 'content_chunk', 'content': content})}\n\n"
                            elif chunk["type"] == "thinking":
                                thought = chunk["content"]
                                thinking += thought
                                # Refined filter: always capture, only hide chunks that strictly contain tool signatures
                                # to prevent UI pollution without silencing the model's logic.
                                chunk_is_blocked = any(_re_module.search(sig, thought.lower()) for sig in get_block_signatures())
                                if not chunk_is_blocked:
                                    yield f"data: {json.dumps({'type': 'thinking_chunk', 'content': thought})}\n\n"
                                    # Live Activity Pulse: sync "Thinking" status to Agent Activity
                                    if len(thinking) % 100 == 0:
                                        yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'thought', 'content': thinking[-120:].strip()})}\n\n"
                            elif chunk["type"] == "error":
                                err_msg = chunk.get('message', 'Unknown error')
                                logger.error(f"[Deep Research] LLM error chunk: {err_msg}")
                                yield f"data: {json.dumps({'type': 'status', 'phase': 'error', 'message': f'LLM error: {err_msg}'})}\n\n"
                                if not raw_content:
                                    raw_content = f"LLM Error during synthesis: {err_msg}"
                                break
                            elif chunk["type"] == "metadata":
                                # Potentially update total_tokens here
                                usage = chunk.get("usage", {})
                                total_tokens += usage.get("total_tokens", usage.get("prompt_tokens", 0) + usage.get("completion_tokens", 0))
                except asyncio.TimeoutError:
                    logger.warning(f"[Deep Research] LLM synthesis stream timed out after {LLM_STREAM_TIMEOUT}s")
                    yield f"data: {json.dumps({'type': 'status', 'phase': 'timeout', 'message': 'LLM synthesis timed out. Finalizing with available data...'})}\n\n"
                except Exception as e:
                    logger.exception(f"[Deep Research] Unexpected error during LLM streaming: {e}")
                    if not raw_content:
                        raw_content = f"Error during synthesis: {str(e)}"

            logger.info(f"[Deep Research] raw_content length after streaming: {len(raw_content)}, preview: {raw_content[:200]}")
            
            # --- Intercept hallucinated tool calls in the eager results path ---
            # Some models (e.g. via OpenRouter) emit tool-call XML instead of a final answer
            # even when no tools are passed. Extract, execute, and re-synthesize.
            from chat.extraction import extract_tool_calls as _extract_tc, strip_tool_calls as _strip_tc
            
            extracted_tcs = _extract_tc(raw_content) if raw_content else []
            if extracted_tcs:
                logger.info(f"[Deep Research] Intercepted {len(extracted_tcs)} hallucinated tool call(s) in eager path. Executing...")
                yield f"data: {json.dumps({'type': 'status', 'phase': 'searching', 'message': f'Model requested {len(extracted_tcs)} additional tool(s). Executing...'})}\n\n"
                
                extra_tool_results = []
                for tc in extracted_tcs:
                    fn = tc['tool']
                    ag = tc['args']
                    if not isinstance(ag, dict):
                        ag = parse_tool_arguments(ag)
                    ag = _sanitize_tool_args(ag)
                    
                    # Default query to user content if missing
                    if fn == "web_search" and not ag.get("query"):
                        ag["query"] = clean_content
                    
                    tool_trace.append({"tool": fn, "args": ag, "iteration": "eager-extra"})
                    yield f"data: {json.dumps({'type': 'agent_trace', 'sub_type': 'tool', 'tool': fn, 'args': ag, 'iteration': 'eager-extra'})}\n\n"
                    
                    ctx = {"user_id": request.user.id}
                    try:
                        res = await asyncio.wait_for(
                            shared_tools.execute_tool(fn, ag, ctx),
                            timeout=TOOL_EXECUTION_TIMEOUT
                        )
                        
                        # Handle web_search results specially (extract sources/images)
                        if fn == "web_search":
                            meta['search_query'] = ag.get("query", clean_content)
                            try:
                                pr = json.loads(res)
                                if pr.get("type") == "search_results":
                                    existing = meta.get('sources', [])
                                    seen = {s.get('url') for s in existing}
                                    for src in pr.get('sources', []):
                                        if src.get('url') not in seen:
                                            existing.append(src)
                                            seen.add(src.get('url'))
                                    meta['sources'] = existing
                                    yield f"data: {json.dumps({'type': 'sources_update', 'sources': existing})}\n\n"
                                    extra_tool_results.append(f"[Tool: {fn}(query=\"{ag.get('query', '')}\")]\nResult: {pr.get('text', '')}")
                                else:
                                    extra_tool_results.append(f"[Tool: {fn}]\nResult: {res}")
                            except Exception:
                                extra_tool_results.append(f"[Tool: {fn}]\nResult: {res}")
                        else:
                            extra_tool_results.append(f"[Tool: {fn}]\nResult: {res}")
                    except asyncio.TimeoutError:
                        extra_tool_results.append(f"[Tool: {fn}] Timeout error")
                    except Exception as e:
                        extra_tool_results.append(f"[Tool: {fn}] Error: {str(e)}")
                
                # Re-synthesize with the extra tool results
                if extra_tool_results:
                    extra_context = "\n\n".join(extra_tool_results)
                    resynthesis_prompt = (
                        f"{prompt_ctx}\n\n"
                        f"Additional tool results from your requested tool calls:\n{extra_context}\n\n"
                        f"Now provide your FINAL answer in the requested JSON format. Do NOT call any more tools."
                    )
                    yield f"data: {json.dumps({'type': 'status', 'phase': 'generating', 'message': 'Synthesizing with additional data...'})}\n\n"
                    try:
                        resynth_result = await execute_llm(
                            provider=provider, model=model, prompt=resynthesis_prompt,
                            system_message=system_message, user_id=request.user.id, history=history_list,
                            response_format=response_format,
                            stream=False,
                        )
                        raw_content = resynth_result.get("content") or ""
                        total_tokens += resynth_result.get("usage", {}).get("total_tokens", 0)
                        if resynth_result.get('thinking'):
                            thinking += resynth_result['thinking'] + "\n\n"
                        logger.info(f"[Deep Research] Re-synthesis result length: {len(raw_content)}")
                    except Exception as e:
                        logger.exception(f"[Deep Research] Re-synthesis LLM call failed: {e}")
                        raw_content = ""
            else:
                # No tool calls found — but strip any residual tool syntax just in case
                stripped = _strip_tc(raw_content).strip() if raw_content else ""
                if stripped != raw_content.strip():
                    logger.info(f"[Deep Research] Stripped residual tool syntax. Before: {len(raw_content)}, After: {len(stripped)}")
                    raw_content = stripped
            
            # If streaming produced no content, build a direct (non-streaming) fallback
            if not raw_content or len(raw_content.strip()) < 10:
                logger.warning(f"[Deep Research] Streaming produced no/minimal content. Falling back to non-streaming LLM call.")
                yield f"data: {json.dumps({'type': 'status', 'phase': 'generating', 'message': 'Retrying synthesis (non-streaming)...'})}\n\n"
                try:
                    # Explicitly tell the LLM NOT to use tools
                    no_tool_prompt = (
                        f"{prompt_ctx}\n\n"
                        f"IMPORTANT: Do NOT call any tools or functions. You already have all the research data above. "
                        f"Provide your FINAL answer in the requested JSON format immediately."
                    )
                    fallback_result = await execute_llm(
                        provider=provider, model=model, prompt=no_tool_prompt,
                        system_message=system_message, user_id=request.user.id, history=history_list,
                        response_format=response_format,
                        attachments=active_attachments,
                        stream=False,
                    )
                    raw_content = fallback_result.get("content") or ""
                    total_tokens += fallback_result.get("usage", {}).get("total_tokens", 0)
                    if fallback_result.get('thinking'):
                        thinking += fallback_result['thinking'] + "\n\n"
                    logger.info(f"[Deep Research] Fallback LLM result length: {len(raw_content)}")
                    
                    # Strip tool calls from fallback too  
                    if raw_content:
                        fallback_extracted = _extract_tc(raw_content)
                        if fallback_extracted:
                            raw_content = _strip_tc(raw_content).strip()
                            logger.warning(f"[Deep Research] Fallback also contained tool calls! Stripped. Remaining: {len(raw_content)}")
                except Exception as e:
                    logger.exception(f"[Deep Research] Fallback LLM call also failed: {e}")
                    raw_content = ""
            
            # total_tokens is already updated inside the loop for simple chat
        else:
            # ---- Agentic Tool Loop (LangGraph) ----
            from chat.graph import run_agent_loop
            import asyncio as _aio

            accumulated_tool_context = []
            _event_queue = _aio.Queue()

            async def _stream_cb(event_type, data):
                await _event_queue.put({"type": event_type, **data})

            yield f"data: {json.dumps({'type': 'status', 'phase': 'thinking', 'message': 'Thinking...'})}\n\n"

            async def _run_graph():
                return await run_agent_loop(
                    full_prompt=full_prompt,
                    metadata=meta,
                    provider=provider,
                    model=model,
                    system_message=system_message,
                    user_id=request.user.id,
                    response_format=response_format,
                    clean_content=clean_content,
                    intent=intent,
                    history_list=history_list,
                    attachments=[],
                    stream_callback=_stream_cb,
                    max_iterations=resolve_agent_iteration_limit(intent),
                )

            _graph_task = _aio.create_task(_run_graph())

            while not _graph_task.done():
                try:
                    ev = await _aio.wait_for(_event_queue.get(), timeout=0.3)
                    yield f"data: {json.dumps(ev)}\n\n"
                except _aio.TimeoutError:
                    continue

            # Drain remaining events
            while not _event_queue.empty():
                ev = await _event_queue.get()
                yield f"data: {json.dumps(ev)}\n\n"

            try:
                graph_result = _graph_task.result()
            except Exception as e:
                logger.exception(f"[LangGraph Streaming] Graph failed: {e}")
                graph_result = {"raw_content": f"Error: {str(e)}", "metadata": meta, "tool_trace": [], "thinking": "", "total_tokens": 0, "interrupted": True, "accumulated_tool_context": []}

            raw_content = graph_result["raw_content"]
            meta = graph_result["metadata"]
            tool_trace = graph_result["tool_trace"]
            thinking = graph_result["thinking"]
            total_tokens += graph_result["total_tokens"]
            accumulated_tool_context = graph_result["accumulated_tool_context"]
            interrupted = graph_result["interrupted"]

        # ---- Finalize ----
        try:
            if tool_trace:
                meta['tool_trace'] = tool_trace

            if raw_content is None:
                raw_content = ""
            
            if thinking:
                meta['thinking'] = thinking
            
            tool_context_text = "\n\n".join(accumulated_tool_context) if accumulated_tool_context else "\n\n".join(eager_results)
            raw_content, normalize_tokens = await ensure_final_response_payload(
                raw_content=raw_content,
                full_prompt=full_prompt,
                provider=provider,
                model=model,
                system_message=system_message,
                user_id=request.user.id,
                response_format=response_format,
                tool_context_text=tool_context_text,
                thinking=thinking,
            )
            total_tokens += normalize_tokens

            # Normalize final LLM output into canonical payload
            payload = normalize_llm_payload(raw_content, provider, model, tool_context_text, meta, llm_result=llm_result)

            response_text = payload.get('response', '')

            # Merge payload metadata into meta
            payload_meta = payload.copy()
            payload_meta.pop('response', None)
            inner_meta = payload_meta.pop('metadata', {}) if isinstance(payload_meta.get('metadata', {}), dict) else {}
            meta.update(inner_meta)

            for k in ('follow_ups', 'thinking', 'sources', 'images', 'videos', 'tool_trace'):
                if payload_meta.get(k) is not None:
                    meta[k] = payload_meta.get(k)

            # Add interruption metadata if process was interrupted
            if interrupted:
                meta['interrupted'] = True
                meta['partial_results'] = "\n\n".join(accumulated_tool_context)[:3000] if accumulated_tool_context else ""
                meta['iterations_completed'] = iteration + 1
                meta['max_iterations'] = actual_max_iterations

        except Exception as finalize_err:
            logger.exception(f"[Finalize] Error during response normalization: {finalize_err}")
            # Emergency fallback: use whatever raw_content we have
            response_text = str(raw_content or '').strip()
            if not response_text or response_text.startswith('Error:') or response_text.startswith('LLM Error'):
                # Build a meaningful fallback from gathered research data
                source_summary = ""
                if meta.get('sources'):
                    top_sources = meta['sources'][:5]
                    source_lines = [f"- [{s.get('title', 'Source')}]({s.get('url', '#')})" for s in top_sources]
                    source_summary = "\n\n**Sources found:**\n" + "\n".join(source_lines)
                response_text = (
                    "I encountered an error while synthesizing the research results, but I was able to gather the sources shown above. "
                    "Please try again or rephrase your query for a fresh attempt."
                    + source_summary
                )

        # Ensure response_text is never empty after finalization
        if not response_text or not response_text.strip():
            logger.warning(f"[Finalize] response_text is empty after normalization. raw_content preview: {str(raw_content)[:300]}")
            source_summary = ""
            if meta.get('sources'):
                top_sources = meta['sources'][:5]
                source_lines = [f"- [{s.get('title', 'Source')}]({s.get('url', '#')})" for s in top_sources]
                source_summary = "\n\n**Sources found:**\n" + "\n".join(source_lines)
            response_text = (
                "I was unable to generate a synthesized response for this research query. "
                "The sources have been gathered and are shown above. "
                "Please try again — this may be a temporary issue with the AI model."
                + source_summary
            )

        session.total_tokens_used += total_tokens
        await session.asave(update_fields=['total_tokens_used'])

        meta['tokens'] = total_tokens

        msg_type = 'chat'
        if 'workflow_id' in meta:
            msg_type = 'workflow_suggestion'
        elif 'search_query' in meta:
            msg_type = 'search'
        elif intent == 'image':
            msg_type = 'image'
        elif intent == 'video':
            msg_type = 'video'

        ai_msg = await ChatMessage.objects.acreate(
            session=session, role='assistant', content=response_text,
            message_type=msg_type,
            metadata=meta,
        )

        # PERSIST GENERATION (Save to Documents/RAG)
        try:
            await persist_llm_generation(request.user, session, ai_msg)
        except Exception as persist_err:
            logger.error(f"[Finalize] persist_llm_generation failed: {persist_err}")

        # [TWO-STAGE FEEDING]: Truncate long assistant messages for context limit management
        word_count = len(response_text.split())
        if word_count > ASSISTANT_SUMMARY_WORD_LIMIT:
            # Create a smart, clean user-friendly preview
            
            # --- Smart Conclusion Extraction ---
            # Try to find a 'Bottom Line' or 'Conclusion' or 'Summary' section at the end first
            conclusion_patterns = [
                r'(?i)\n(?:###|##|#|\*\*)\s*(Bottom Line|Conclusion|Summary|Final Takeaway)[:\s\n]+(.*?)$',
                r'(?i)\n(?:Bottom Line|In summary|To conclude)[:\s\n]+(.*?)$'
            ]
            
            smart_summary = None
            for pattern in conclusion_patterns:
                match = _re_module.search(pattern, response_text, _re_module.DOTALL)
                if match:
                    # It found a likely conclusion section! Use it.
                    smart_summary = match.group(match.lastindex).strip()
                    break
            
            if not smart_summary or len(smart_summary.split()) < 10:
                # No clear conclusion found, or it was too short. Fall back to cleaned beginning.
                clean_source = response_text
            else:
                # We have a smart summary! Prepend a label
                clean_source = f"[Key Points]: {smart_summary}"
            
            # --- Text Cleaning ---
            # Strip markdown artifacts (pipes, tables, hashes, bold markers etc.)
            clean_text = _re_module.sub(r'[*#_`|\[\]]', '', clean_source)
            clean_text = _re_module.sub(r'-{3,}', ' ', clean_text)
            clean_text = _re_module.sub(r'\s+', ' ', clean_text).strip()
            
            # Truncate to a manageable word limit for the preview accordion
            words = clean_text.split()
            if len(words) > 130:
                preview_content = " ".join(words[:130]) + "... [Quick Preview]"
            else:
                preview_content = clean_text
            
            ai_msg.metadata['summary'] = preview_content
            await ai_msg.asave(update_fields=['metadata'])
            logger.info(f"Generated smart friendly preview for message {ai_msg.id} ({word_count} words)")

        yield f"data: {json.dumps({'type': 'done', 'user_message': await serialize_message(user_msg), 'ai_response': await serialize_message(ai_msg)})}\n\n"

    response = StreamingHttpResponse(event_stream(), content_type='text/event-stream')
    response['Cache-Control'] = 'no-cache'
    response['X-Accel-Buffering'] = 'no'
    return response


async def persist_llm_generation(user, session, ai_msg):
    """
    Save generated media content (image, video) as a Document
    and ChatAttachment. 
    
    NOTE: Long text responses are NOT saved to RAG anymore.
    The summary feature (text truncation) handles context 
    management for long assistant messages instead.
    """
    from inference.models import Document
    from chat.models import ChatAttachment
    from django.core.files.base import ContentFile
    import httpx
    from uuid import uuid4

    meta = ai_msg.metadata or {}
    media_url = meta.get('media_url')
    msg_type = ai_msg.message_type

    # Only persist actual generated media (images, videos), not text responses
    if not (media_url and msg_type in ('image', 'video')):
        return

    doc = None
    filename = f"generated_{uuid4().hex[:8]}"
    
    try:
        ext = ".png" if msg_type == 'image' else ".mp4"
        filename += ext
        
        async with httpx.AsyncClient() as client:
            resp = await client.get(media_url, timeout=30)
            if resp.status_code == 200:
                doc = await sync_to_async(Document.objects.create)(
                    user=user,
                    name=filename,
                    file_type=msg_type,
                    file_size=len(resp.content),
                    status='pending'
                )
                @sync_to_async
                def save_file(d, cf):
                    d.file.save(filename, cf)
                    d.save()
                
                await save_file(doc, ContentFile(resp.content))

        if doc:
            attachment = await ChatAttachment.objects.acreate(
                session=session,
                message=ai_msg,
                filename=filename,
                file=doc.file,
                file_type=msg_type,
                file_size=doc.file_size,
                inference_document=doc,
            )
            
            meta['attachment_id'] = str(attachment.id)
            ai_msg.metadata = meta
            await ai_msg.asave(update_fields=['metadata'])
            logger.info(f"Persisted {msg_type} generation as document {doc.id} and attachment {attachment.id}")

    except Exception as e:
        logger.error(f"Failed to persist generation: {e}", exc_info=True)



def parse_llm_json_response(raw: Any) -> tuple[str, list[str], str, str]:
    """
    Parse structured JSON response from LLM.
    Expected format: {"response": "...", "follow_ups": ["...", ...], "thinking": "..."}
    
    Returns (content, follow_ups, thinking, summary). Gracefully falls back if JSON parsing fails.
    """
    
    if not raw:
        return "", [], "", ""

    # DeepSeek R1 and similar sometimes output <think> tags completely outside the JSON payload.
    # We must extract this from the raw string *before* truncating to JSON braces.
    thinking_outside = ""
    if isinstance(raw, str):
        think_matches = _re_module.findall(r'<(?:think|thought)>(.*?)</(?:think|thought)>', raw, _re_module.DOTALL | _re_module.IGNORECASE)
        if think_matches:
            thinking_outside = "\n\n".join([m.strip() for m in think_matches if m.strip()])

    def extract_from_dict(data: dict) -> tuple[str, list[str], str, str]:
        content = data.get('response', '')
        follow_ups = data.get('follow_ups', [])
        # Provide external thinking if the JSON didn't include it gracefully
        thinking = data.get('thinking', '') or thinking_outside
        summary = data.get('summary', '')
        
        # If the LLM returned JSON but forgot the 'response' wrapper key
        if not content and len(data) > 0 and 'response' not in data:
            for key in ['content', 'text', 'answer']:
                if key in data:
                    content = data[key]
                    break
            else:
                # If the dict looks like an error object, summarize it instead of dumping raw JSON
                if data.get('status') == 'error' or data.get('Error') or data.get('error'):
                    err_msg = data.get('error') or data.get('Error') or data.get('message') or "An unknown tool execution error occurred."
                    content = f"I encountered an issue while trying to process your request: {err_msg}"
                else:
                    # Fallback to dumping whatever dict it created if no clear text key found
                    content = json.dumps(data, indent=2)

        # Validate types
        if not isinstance(content, str):
            content = str(content)
        if not isinstance(follow_ups, list):
            follow_ups = []
        follow_ups = [str(q) for q in follow_ups if q][:5]
        if not isinstance(thinking, str):
            thinking = str(thinking) if thinking else ""
        if not isinstance(summary, str):
            summary = str(summary) if summary else ""
            
        # Proactively strip any lingering tool calls from the content field
        if content:
            content = strip_tool_calls(content)
            
        return content, follow_ups, thinking, summary

    # 1. Handle case where it's already a dict
    if isinstance(raw, dict):
        return extract_from_dict(raw)

    # 2. Handle string parsing
    if not isinstance(raw, str):
        raw = str(raw)

    text = raw.strip()
    
    # Try finding markdown code block with regex, even if there's text around it
    fence_match = _re_module.search(r'```(?:json)?\s*\n?(.*?)\n?```', text, _re_module.DOTALL | _re_module.IGNORECASE)
    # Try finding custom <json_response> tags
    tag_match = _re_module.search(r'<json_response>(.*?)</json_response>', text, _re_module.DOTALL | _re_module.IGNORECASE)
    
    if tag_match:
        text = tag_match.group(1).strip()
    elif fence_match:
        text = fence_match.group(1).strip()
    else:
        # Try to extract JSON via braces if it's embedded in other text
        start_idx = text.find('{')
        if start_idx != -1:
            brace_count = 0
            in_string = False
            escape = False
            valid_json_str = ""
            for char in text[start_idx:]:
                valid_json_str += char
                if char == '"' and not escape:
                    in_string = not in_string
                if not in_string:
                    if char == '{':
                        brace_count += 1
                    elif char == '}':
                        brace_count -= 1
                        if brace_count == 0:
                            break
                if char == '\\' and not escape:
                    escape = True
                else:
                    escape = False
            
            if brace_count == 0 and valid_json_str:
                text = valid_json_str
            else:
                end_idx = text.rfind('}')
                if end_idx != -1 and end_idx > start_idx:
                    text = text[start_idx:end_idx+1]

    try:
        # strict=False allows unescaped control characters inside JSON strings
        data = json.loads(text, strict=False)
        return extract_from_dict(data)
    except (json.JSONDecodeError, AttributeError, TypeError) as e:
        # Fallback 1: Try fuzzy_json_loads for python-style dicts (single quotes)
        fuzzy_data = fuzzy_json_loads(text)
        if isinstance(fuzzy_data, dict) and ('response' in fuzzy_data or 'content' in fuzzy_data or 'answer' in fuzzy_data):
            return extract_from_dict(fuzzy_data)

        # If it's a string that doesn't even look like JSON (no braces or tags), 
        # treat it as a silent plain-text fallback instead of warning.
        if isinstance(raw, str) and "{" not in raw and "<json_response>" not in raw:
            # Still check for <think> tags in plain text
            thinking = ""
            think_match = _re_module.search(r'<think>(.*?)</think>', raw, _re_module.DOTALL | _re_module.IGNORECASE)
            if think_match:
                thinking = think_match.group(1).strip()
                clean_content = _re_module.sub(r'<think>.*?</think>', raw, flags=_re_module.DOTALL | _re_module.IGNORECASE).strip()
                # Ensure tools are stripped from fallback content
                clean_content = strip_tool_calls(clean_content)
                return clean_content, [], thinking, ""
            
            clean_raw = strip_tool_calls(raw.strip())
            return clean_raw, [], "", ""

        logger.warning(f"LLM did not return valid JSON: {str(e)}. Attempting regex fallback on: {raw[:200]}...")
        
        # We know raw is a string here from the check above
        clean_raw = raw.strip()
        
        # Regex fallback to extract "response", "follow_ups", and "thinking"
        thinking = ""
        think_match = _re_module.search(r'[\'"]thinking[\'"]\s*:\s*[\'"](.*?)[\'"]', clean_raw, _re_module.DOTALL | _re_module.IGNORECASE)
        if think_match:
            thinking = think_match.group(1).replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')

        resp_match = _re_module.search(r'[\'"]response[\'"]\s*:\s*[\'"](.*?)[\'"](?:\s*,\s*[\'"]follow_ups[\'"]|\s*,\s*[\'"]thinking[\'"]|\s*\})', clean_raw, _re_module.DOTALL | _re_module.IGNORECASE)
        if resp_match:
            content = resp_match.group(1)
            content = content.replace('\\n', '\n').replace('\\"', '"').replace("\\'", "'").replace('\\\\', '\\')
            
            follow_ups = []
            fu_match = _re_module.search(r'[\'"]follow_ups[\'"]\s*:\s*\[(.*?)\]', clean_raw, _re_module.DOTALL | _re_module.IGNORECASE)
            if fu_match:
                fu_text = fu_match.group(1)
                follow_ups = _re_module.findall(r'[\'"]([^\'"]+)[\'"]', fu_text)
                
            return content, follow_ups[:5], thinking, ""

        # If thinking wasn't in JSON, maybe it's in <think> tags
        if not thinking:
            think_tag_match = _re_module.search(r'<think>(.*?)</think>', clean_raw, _re_module.DOTALL | _re_module.IGNORECASE)
            if think_tag_match:
                thinking = think_tag_match.group(1).strip()
                clean_raw = _re_module.sub(r'<think>.*?</think>', '', clean_raw, flags=_re_module.DOTALL | _re_module.IGNORECASE).strip()

        # If the model mixed tool-call syntax into final text, strip it and keep the user-facing content.
        mixed_tool_calls = extract_tool_calls(clean_raw)
        if mixed_tool_calls:
            cleaned_text = strip_tool_calls(clean_raw).strip()
            pending_action_phrases = _re_module.search(
                r"\b(i(?:'| )?ll|i will|let me)\s+(search|look up|use|fetch|check|call)\b",
                cleaned_text,
                _re_module.IGNORECASE,
            )
            if cleaned_text and not pending_action_phrases:
                return cleaned_text, [], thinking
            return "I ran tools but could not format a final response yet. Please retry once.", [], thinking

        # If regex completely fails, strip random JSON wrappers and return it directly
        clean_raw = _re_module.sub(r'^```[a-z]*\s*\n|\n```$', '', clean_raw).strip()
        
        # Last attempt to strip dangling JSON map pieces
        if clean_raw.startswith('{"response":'):
            clean_raw = _re_module.sub(r'^\{"response":\s*"', '', clean_raw)
            clean_raw = _re_module.sub(r'"(?:,\s*"follow_ups"\s*:\s*\[.*?\]\s*)?(?:,\s*"thinking"\s*:\s*".*?"\s*)?\}?$', '', clean_raw)
            clean_raw = clean_raw.replace('\\n', '\n').replace('\\"', '"')

        # Final safety check for tools on the cleaned string
        clean_raw = strip_tool_calls(clean_raw)

        return clean_raw, [], thinking, ""



# ==================== Delete Message ====================
@sync_api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def delete_message(request, session_id: str, message_id: int):
    """
    Delete a specific message from a chat session.
    If the message represents a file upload and has an attachment ID,
    the underlying ChatAttachment is also deleted.
    """
    logger.error(f"HIT DELETE MESSAGE Endpoint: session={session_id}, msg={message_id}, user={request.user}")
    try:
        session_uuid = UUID(session_id)
        session = ChatSession.objects.filter(id=session_uuid, user=request.user).first()
    except ValueError as e:
        logger.error(f"Value error parsing session id: {e}")
        return Response({'error': 'Invalid session ID format'}, status=400)

    if not session:
        logger.error(f"Chat session not found for id {session_id} and user {request.user}")
        return Response({'error': 'Chat session not found'}, status=404)

    logger.error(f"Session found: {session.id}. Looking for message {message_id}")
    msg = get_object_or_404(ChatMessage, id=message_id, session=session)
    logger.error(f"Message found: {msg.id}")
    
    # Check for rewind parameter (delete this message and all subsequent messages)
    is_rewind = request.query_params.get('rewind', '').lower() == 'true'
    is_rewind_after = request.query_params.get('rewind_after', '').lower() == 'true'

    if is_rewind or is_rewind_after:
        logger.info(f"Rewinding session {session_id} from message {message_id} (after={is_rewind_after})")
        # Find all messages that came at the same time or after (or just id >= since sequential)
        # Using id is safer and faster since they are auto-incrementing integers
        if is_rewind_after:
            messages_to_delete = ChatMessage.objects.filter(session=session, id__gt=message_id)
        else:
            messages_to_delete = ChatMessage.objects.filter(session=session, id__gte=message_id)
        
        # Also clean up their attachments if any
        for m in messages_to_delete:
            att_id = m.metadata.get('attachment_id')
            if att_id:
                try:
                    att = ChatAttachment.objects.filter(id=UUID(att_id), session=session).first()
                    if att:
                        if att.file:
                            try:
                                att.file.delete(save=False)
                            except Exception:
                                pass
                        # Hybrid RAG Cleanup: also clean up inference docs on rewind
                        if att.inference_document_id:
                            inf_doc_id = att.inference_document_id
                            try:
                                from inference.models import Document as InfDoc
                                from inference.engine import get_session_knowledge_base
                                session_kb = get_session_knowledge_base(session_id)
                                try:
                                    from asgiref.sync import async_to_sync
                                    async_to_sync(session_kb.delete_document)(inf_doc_id)
                                except Exception as e:
                                    logger.warning(f"Failed to remove rewind doc from Session KB: {e}")
                                InfDoc.objects.filter(id=inf_doc_id).delete()
                            except Exception as e:
                                logger.error(f"Rewind RAG cleanup failed: {e}")
                        att.delete()
                except ValueError:
                    pass
        
        messages_to_delete.delete()
        return Response({'status': 'Conversation rewound successfully.'}, status=status.HTTP_204_NO_CONTENT)
    else:
        # Standard logic: Check if this single message is tied to an attachment
        attachment_id = msg.metadata.get('attachment_id')
        if attachment_id:
            try:
                att_uuid = UUID(attachment_id)
                attachment = ChatAttachment.objects.filter(id=att_uuid, session=session).first()
                if attachment:
                    if attachment.file:
                        try:
                            attachment.file.delete(save=False)
                        except Exception as e:
                            logger.warning(f"Failed to delete attachment file from disk: {e}")
                            
                    # Hybrid RAG Cleanup: Delete inference.Document and remove from Session KB
                    if attachment.inference_document_id:
                        inf_doc_id = attachment.inference_document_id
                        try:
                            from inference.models import Document
                            # Delete from FAISS memory
                            from inference.engine import get_session_knowledge_base
                            session_kb = get_session_knowledge_base(session_id)
                            import asyncio
                            try:
                                from asgiref.sync import async_to_sync
                                async_to_sync(session_kb.delete_document)(inf_doc_id)
                            except Exception as e:
                                logger.warning(f"Failed to delete FAISS document from Session KB: {e}")
                                
                            # Delete SQL record (cascades Chunks)
                            Document.objects.filter(id=inf_doc_id).delete()
                        except Exception as e:
                            logger.error(f"Failed to clean up inference document: {e}")
                            
                    attachment.delete()
            except ValueError:
                pass

        # Delete the single message itself
        msg.delete()
        return Response({'status': 'Message deleted successfully.'}, status=status.HTTP_204_NO_CONTENT)


# ==================== File Upload ====================
@sync_api_view(['POST'])
@permission_classes([IsAuthenticated])
@parser_classes([MultiPartParser, FormParser])
def upload_file(request, session_id: str):
    """
    Upload a file (image, PDF, PPT, text) to a chat session.
    Extracts text from documents and stores it for context injection.
    """
    try:
        session_uuid = UUID(session_id)
        session = ChatSession.objects.filter(id=session_uuid, user=request.user).first()
    except ValueError:
        return Response({'error': 'Invalid session ID format'}, status=400)

    if not session:
        return Response({'error': 'Chat session not found'}, status=404)

    file = request.FILES.get('file')
    if not file:
        return Response({'error': 'No file provided'}, status=400)

    # Determine file type
    filename = file.name.lower()
    if filename.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.bmp')):
        file_type = 'image'
    elif filename.endswith('.pdf'):
        file_type = 'pdf'
    elif filename.endswith(('.pptx', '.ppt')):
        file_type = 'pptx'
    elif filename.endswith(('.txt', '.md', '.csv', '.json', '.xml', '.html')):
        file_type = 'text'
    else:
        file_type = 'other'

    # Extract text content
    extracted_text = ""
    file_bytes = file.read()
    file.seek(0)
    
    if file_type == 'pdf':
        extracted_text = _extract_pdf_text_sync(file_bytes)
    elif file_type == 'pptx':
        extracted_text = _extract_pptx_text_sync(file_bytes)
    elif file_type == 'text':
        try:
            extracted_text = file_bytes.decode('utf-8', errors='ignore')
        except Exception:
            extracted_text = ""

    # Save attachment
    attachment = ChatAttachment.objects.create(
        session=session,
        file=file,
        filename=file.name,
        file_type=file_type,
        file_size=file.size,
        extracted_text=extracted_text[:DOCUMENT_EXTRACT_CAP],  # Cap at 500K chars
        is_large_file=len(extracted_text) > IS_LARGE_FILE_THRESHOLD,
    )

    # Hierarchical RAG: Index into User Knowledge Base
    if file_type in ('pdf', 'pptx', 'text') and extracted_text:
        try:
            from inference.models import Document as InferenceDoc
            from inference.tasks import process_document_task

            inf_doc = InferenceDoc.objects.create(
                user=request.user,
                name=file.name,
                content_text=extracted_text,
                file=file,
                file_type=file_type,
                file_size=file.size,
                status='pending'
            )
            attachment.inference_document = inf_doc
            attachment.save(update_fields=['inference_document'])

            # Index into User KB (persistent, read-only from chat sessions)
            process_document_task.delay(inf_doc.id)
        except Exception as e:
            logger.error(f"Failed to trigger RAG indexing for {file.name}: {e}")

    # Create a system message noting the upload
    content_preview = ""
    if extracted_text:
        # [TWO-TIERED CONTEXT]: Create a ~120,000 character summary/preview immediately
        content_preview = f"\n\nContext Summary ({len(extracted_text)} chars):\n{extracted_text[:LARGE_FILE_PREVIEW_LENGTH]}..."

    ChatMessage.objects.create(
        session=session,
        role='system',
        content=f"📎 File uploaded: **{file.name}** ({file_type}, {file.size} bytes){content_preview}",
        message_type='system',
        metadata={
            'attachment_id': str(attachment.id),
            'file_type': file_type,
            'has_extracted_text': bool(extracted_text),
        },
    )

    from .serializers import ChatAttachmentSerializer
    return Response({
        'attachment': ChatAttachmentSerializer(attachment).data,
        'extracted_text_length': len(extracted_text),
        'message': f'File "{file.name}" uploaded successfully.',
    })


def _extract_pdf_text_sync(file_bytes: bytes) -> str:
    """Extract text from a PDF file."""
    try:
        import io
        try:
            from PyPDF2 import PdfReader
            reader = PdfReader(io.BytesIO(file_bytes))
            text_parts = []
            for page in reader.pages[:100]:  # Limit to 100 pages
                text = page.extract_text()
                if text:
                    text_parts.append(text)
            return "\n\n".join(text_parts)
        except ImportError:
            return "[PDF extraction requires PyPDF2. Install with: pip install PyPDF2]"
    except Exception as e:
        logger.error(f"PDF extraction failed: {e}")
        return ""


def _extract_pptx_text_sync(file_bytes: bytes) -> str:
    """Extract text from a PowerPoint file."""
    try:
        import io
        try:
            from pptx import Presentation
            prs = Presentation(io.BytesIO(file_bytes))
            text_parts = []
            for slide in prs.slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        text_parts.append(shape.text)
            return "\n\n".join(text_parts)
        except ImportError:
            return "[PPTX extraction requires python-pptx. Install with: pip install python-pptx]"
    except Exception as e:
        logger.error(f"PPTX extraction failed: {e}")
        return ""


# ==================== Run Workflow from Chat ====================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
async def run_workflow_from_chat(request, session_id: str):
    """
    Execute a workflow suggested by the AI assistant.
    Called when the user clicks "Approve & Run" on a workflow suggestion.
    """
    try:
        session_uuid = UUID(session_id)
        session = await ChatSession.objects.filter(id=session_uuid, user=request.user).afirst()
    except ValueError:
        return Response({'error': 'Invalid session ID format'}, status=400)

    if not session:
        return Response({'error': 'Chat session not found'}, status=404)

    workflow_id = request.data.get('workflow_id')
    if not workflow_id:
        return Response({'error': 'workflow_id is required'}, status=400)

    from orchestrator.models import Workflow
    from executor.king import get_orchestrator
    from asgiref.sync import sync_to_async

    workflow = await Workflow.objects.filter(id=workflow_id, user=request.user).afirst()
    if not workflow:
        return Response({'error': 'Workflow not found'}, status=404)

    # Build workflow JSON
    workflow_json = {
        'id': workflow.id,
        'nodes': workflow.nodes,
        'edges': workflow.edges,
        'settings': workflow.workflow_settings,
    }

    # Get credentials
    from executor.credential_utils import get_workflow_credentials
    active_creds = await sync_to_async(get_workflow_credentials)(request.user.id, workflow_json)

    # Start execution
    orchestrator = get_orchestrator(request.user.id)
    try:
        handle = await orchestrator.start(
            workflow_json=workflow_json,
            user_id=request.user.id,
            input_data={},
            credentials=active_creds,
            supervision=workflow.supervision_level,
            context=workflow.context,
        )
    except Exception as e:
        logger.error(f"Workflow execution from chat failed: {e}")
        
        ai_msg = await ChatMessage.objects.acreate(
            session=session,
            role='assistant',
            content=f"❌ Failed to start workflow: {str(e)}",
            message_type='workflow_result',
            metadata={'workflow_id': workflow_id, 'error': str(e)},
        )
        return Response({
            'ai_response': await serialize_message(ai_msg),
            'error': str(e),
        }, status=400)

    # Save execution started message
    ai_msg = await ChatMessage.objects.acreate(
        session=session,
        role='assistant',
        content=(
            f"🚀 **Workflow started!**\n\n"
            f"**{workflow.name}** is now running.\n"
            f"Execution ID: `{handle.execution_id}`\n\n"
            f"You can track progress in the Executions panel."
        ),
        message_type='workflow_result',
        metadata={
            'workflow_id': workflow_id,
            'workflow_name': workflow.name,
            'execution_id': str(handle.execution_id),
        },
    )

    return Response({
        'ai_response': await serialize_message(ai_msg),
        'execution_id': str(handle.execution_id),
        'workflow_name': workflow.name,
    })
