"""
Shared Tool Registry for Agentic Execution
"""
import json
import logging
import re
import asyncio
from typing import Any, Dict, List

from core.net import validate_url
from workflow_backend.thresholds import (
    READ_URL_CHAR_LIMIT,
    HISTORY_SEARCH_MAX_MATCHES,
    HISTORY_SEARCH_SNIPPET_CHARS,
    HISTORY_SEARCH_MAX_TOTAL_CHARS,
    HISTORY_SEARCH_MAX_PATTERN_LEN,
    HISTORY_SEARCH_SCAN_LIMIT,
    HTML_ARTIFACT_MAX_CHARS,
    HTML_ARTIFACT_MAX_WIDTH,
    HTML_ARTIFACT_MAX_HEIGHT,
    HTML_ARTIFACT_MIN_WIDTH,
    HTML_ARTIFACT_MIN_HEIGHT,
    HTML_ARTIFACT_DEFAULT_WIDTH,
    HTML_ARTIFACT_DEFAULT_HEIGHT,
)

logger = logging.getLogger(__name__)

# Tools that require Human-In-The-Loop approval before execution.
#
# The shell/file/python entries are gone along with the tools themselves — chat
# no longer touches the filesystem or executes code. What remains is the set that
# can still act on the user's behalf: drive their browser session or reach an
# internal endpoint.
#
# render_html_artifact is deliberately NOT here. It renders inside a sandboxed
# iframe with no network, no same-origin access and no session, so there is
# nothing for a human to meaningfully approve — and prompting on every chart
# would train users to click through approvals without reading them, which is
# what makes the prompts on the remaining tools worthless.
SENSITIVE_TOOLS = [
    "frontend_click",
    "frontend_fill",
    "frontend_navigate",
    "call_internal_api"
]


# The SSRF ruleset now lives in core.net, because the workflow HTTP connector
# needs exactly the same guard and a second copy would drift. Kept as a thin
# shim so the existing call sites and any external importer keep working.
class SSRFValidator:
    validate = staticmethod(validate_url)


class ToolExecutor:
    # _is_safe_path lived here to keep the file tools away from .env/.ssh/*.pem.
    # Those tools are gone, and a path allow-list with no callers is worse than
    # no allow-list: it reads like the filesystem is still guarded, so the next
    # person to add a file tool assumes protection that nothing is applying.

    AVAILABLE_TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": "Search the web to find up-to-date information, news, facts, or references.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The search query to execute on the web."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "deep_research",
                "description": (
                    "Research a topic in depth: plans several search queries, runs them, "
                    "reads the resulting pages and returns their extracted text with "
                    "sources. Use this instead of repeated web_search calls when the "
                    "question needs breadth or corroboration across sources."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "topic": {
                            "type": "string",
                            "description": "The subject to research, stated in full."
                        },
                        "queries": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": (
                                "2-4 distinct search queries covering different angles. "
                                "Omit to derive them from the topic."
                            )
                        },
                        "max_pages": {
                            "type": "integer",
                            "description": "Pages to read, 5-50. Defaults to 15."
                        }
                    },
                    "required": ["topic"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "image_search",
                "description": "Search for specific images visually related to a topic. Run this if the user asks to see photos, diagrams, or visual examples.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The image search query."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "video_search",
                "description": "Search for specific videos related to a topic. Run this if the user asks to see videos, tutorials, or footage.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The video search query."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_current_time",
                "description": "Get the current date and time. Use this when the user asks for the current date, time, or day of the week.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "dispatch_ui_actions",
                "description": "Dispatch one or multiple actions to the user's frontend. Use this to navigate pages, show toasts, or manipulate the ReactFlow canvas (add_node, connect_nodes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action_type": {
                                        "type": "string",
                                        "enum": ["navigate", "show_toast", "open_modal", "add_node", "update_node", "remove_node", "connect_nodes", "disconnect_nodes", "clear_canvas", "replace_canvas"]
                                    },
                                    "payload": {
                                        "type": "object",
                                        "description": "The payload specific to the action_type."
                                    }
                                },
                                "required": ["action_type", "payload"]
                            }
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "call_internal_api",
                "description": "Call any internal REST API endpoint in the platform (e.g., /api/workflows/, /api/credentials/). Returns the JSON response from the server.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "method": { 
                            "type": "string", 
                            "enum": ["GET", "POST", "PUT", "DELETE", "PATCH"],
                            "description": "The HTTP method to use." 
                        },
                        "path": { 
                            "type": "string", 
                            "description": "The URL path (e.g., /api/workflows/, /api/credentials/1/)" 
                        },
                        "data": { 
                            "type": "object", 
                            "description": "JSON payload for POST/PUT/PATCH requests." 
                        },
                        "query_params": { 
                            "type": "object", 
                            "description": "Query parameters for GET requests." 
                        }
                    },
                    "required": ["method", "path"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_url",
                "description": "Fetch and extract text content from a given web page URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to fetch."
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "read_attachment_text",
                "description": "Fetch the full extracted text of a previously uploaded file/attachment from the database. Use this if the preview snippet in the context is insufficient.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "attachment_id": {
                            "type": "string",
                            "description": "The UUID of the attachment to read."
                        }
                    },
                    "required": ["attachment_id"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "get_chat_message_full_text",
                "description": "Fetch the full original content of a previous assistant message that was summarized. Use this if the summary in the history is missing details you need.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "integer",
                            "description": "The ID of the message to read."
                        }
                    },
                    "required": ["message_id"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "list_knowledge_bases",
                "description": "List all knowledge bases (KBs) available to the user. Call this first to discover which KBs exist and their IDs before deciding which one to search. Each KB has a name, document count, and vector count.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "knowledge_base_search",
                "description": (
                    "Search a specific knowledge base (or the user's default KB) using semantic similarity. "
                    "Use this when the user asks about content from their uploaded documents. "
                    "Call list_knowledge_bases first if you are unsure which KB to search. "
                    "Do NOT call this unless the query is genuinely about document content — avoid for factual/coding questions."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural-language query to search for in the knowledge base."
                        },
                        "kb_id": {
                            "type": "integer",
                            "description": "ID of the specific KB to search (from list_knowledge_bases). Omit to search the user's default KB."
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of top results to return (default 5, max 20)."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "scrape_webpage",
                "description": "Scrape a webpage and extract structured content including headings, links, tables, and metadata. More powerful than read_url — use this when you need structured data from a page, not just raw text.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL of the webpage to scrape."
                        },
                        "extract": {
                            "type": "array",
                            "items": {"type": "string", "enum": ["text", "headings", "links", "tables", "metadata", "images"]},
                            "description": "What to extract from the page (default: all). Specify a subset to reduce output size."
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "generate_image",
                "description": "Generate an AI image from a text prompt using an image generation model. Use when the user asks you to create, draw, or generate an image.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "prompt": {
                            "type": "string",
                            "description": "A detailed description of the image to generate."
                        },
                        "size": {
                            "type": "string",
                            "enum": ["256x256", "512x512", "1024x1024", "1024x1792", "1792x1024"],
                            "description": "Image dimensions (default 1024x1024)."
                        },
                        "style": {
                            "type": "string",
                            "enum": ["natural", "vivid"],
                            "description": "Image style (default vivid)."
                        }
                    },
                    "required": ["prompt"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "search_conversation_history",
                "description": (
                    "Search the ENTIRE history of this conversation, including turns that are "
                    "no longer in your visible context and your own earlier reasoning. "
                    "Only the most recent turns are replayed to you automatically — everything "
                    "older is still stored and only reachable through this tool. "
                    "Use it whenever the user refers to something you cannot see ('the number I "
                    "gave you earlier', 'what we decided yesterday'). Prefer this over telling "
                    "the user you do not remember."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": (
                                "Words or a phrase to look for. Matching is case-insensitive and "
                                "term-based: results are ranked by how many of your terms they "
                                "contain, so give several specific words rather than a sentence."
                            )
                        },
                        "scope": {
                            "type": "string",
                            "enum": ["all", "messages", "reasoning"],
                            "description": (
                                "'messages' searches what was said, 'reasoning' searches your own "
                                "stored thinking from earlier turns, 'all' searches both. "
                                "Defaults to 'all'."
                            )
                        },
                        "role": {
                            "type": "string",
                            "enum": ["any", "user", "assistant"],
                            "description": "Restrict to one speaker. Defaults to 'any'."
                        }
                    },
                    "required": ["query"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "render_html_artifact",
                "description": (
                    "Render a self-contained HTML/CSS/JS snippet as a live, interactive card in "
                    "the chat. Use for charts, diagrams, tables, small demos or anything better "
                    "shown than described. The snippet runs in a locked-down sandbox: it has no "
                    "network access, no access to the page around it, and no access to the user's "
                    "session. Inline all CSS and JS — external files will not load."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "html": {
                            "type": "string",
                            "description": "A complete, self-contained HTML document or fragment."
                        },
                        "title": {
                            "type": "string",
                            "description": "Short label shown on the card header."
                        },
                        "width": {
                            "type": "integer",
                            "description": (
                                f"Requested width in px. Clamped to "
                                f"{HTML_ARTIFACT_MIN_WIDTH}-{HTML_ARTIFACT_MAX_WIDTH}."
                            )
                        },
                        "height": {
                            "type": "integer",
                            "description": (
                                f"Requested height in px. Clamped to "
                                f"{HTML_ARTIFACT_MIN_HEIGHT}-{HTML_ARTIFACT_MAX_HEIGHT}."
                            )
                        }
                    },
                    "required": ["html"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "frontend_click",
                "description": "Click an element on the user's active screen. Use this when the user asks you to interact with the UI. You must provide the 'buddy_id' of the element from the screen context.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "buddy_id": {
                            "type": "string",
                            "description": "The data-buddy-id of the element to click."
                        }
                    },
                    "required": ["buddy_id"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "frontend_fill",
                "description": "Type text into an input field or form on the user's active screen. Use this when the user asks you to fill out a form or search bar. You must provide the 'buddy_id' of the input element.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "buddy_id": {
                            "type": "string",
                            "description": "The data-buddy-id of the input element."
                        },
                        "value": {
                            "type": "string",
                            "description": "The text to type into the element."
                        }
                    },
                    "required": ["buddy_id", "value"],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "frontend_navigate",
                "description": "Navigate the user's active screen to a new URL. Use this to open pages within the application.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {
                            "type": "string",
                            "description": "The URL to navigate to."
                        }
                    },
                    "required": ["url"],
                    "additionalProperties": False
                }
            }
        }
    ]

    # Tools that only make sense when the conversation has a memory to consult.
    # Offering these with memory off would be incoherent: the model would call a
    # tool that reads exactly the history we just told it it cannot have.
    MEMORY_DEPENDENT_TOOLS = {"search_conversation_history", "get_chat_message_full_text"}

    @staticmethod
    async def get_available_tools(user_id: int | None, memory_enabled: bool = True) -> List[Dict[str, Any]]:
        """
        Return the full tool list for this user: built-in tools + any MCP tools
        the user has enabled. Safe to call on every agent turn (MCP tool lists
        are cached in Redis).
        """
        tools = list(ToolExecutor.AVAILABLE_TOOLS)
        if not memory_enabled:
            tools = [
                t for t in tools
                if t.get("function", {}).get("name") not in ToolExecutor.MEMORY_DEPENDENT_TOOLS
            ]
        if user_id is None:
            return tools
        try:
            from mcp_integration.tool_provider import MCPToolProvider
            mcp_descriptors = await MCPToolProvider.get_openai_tool_descriptors(user_id)
            tools.extend(mcp_descriptors)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"Could not load MCP tools for user {user_id}: {e}")
        return tools

    @staticmethod
    async def _web_search(args: Dict, context: Dict) -> str:
        from .search import web_search

        query = (args.get("query") or "").strip()
        if not query:
            return "Error: 'query' is required."
        results = await web_search(query)
        return json.dumps({
            "type": "search_results",
            "text": (
                f"Search results for '{query}':\n\n{results.text}" if results.text
                else f"No results found for '{query}'. Try different wording."
            ),
            "sources": list(results.sources),
        })

    @staticmethod
    async def _image_search(args: Dict, context: Dict) -> str:
        from .search import image_search

        query = (args.get("query") or "").strip()
        if not query:
            return "Error: 'query' is required."
        images = await image_search(query)
        return json.dumps({
            "type": "image_results",
            "text": f"Retrieved {len(images)} image(s) for '{query}'; they are shown in the UI.",
            "images": images,
        })

    @staticmethod
    async def _video_search(args: Dict, context: Dict) -> str:
        from .search import video_search

        query = (args.get("query") or "").strip()
        if not query:
            return "Error: 'query' is required."
        videos = await video_search(query)
        return json.dumps({
            "type": "video_results",
            "text": f"Retrieved {len(videos)} video(s) for '{query}'; they are shown in the UI.",
            "videos": videos,
        })

    #: Total extracted text handed back from one deep_research call. Past this
    #: the model starts losing the earlier sources anyway, and the context clamp
    #: would trim it blindly rather than by relevance.
    DEEP_RESEARCH_CHAR_BUDGET = 60_000

    @staticmethod
    async def _deep_research(args: Dict, context: Dict) -> str:
        """
        Breadth-first research: fan out across queries, read the pages, return
        the text with its sources.

        This is the pipeline that used to be inlined in the streaming view and
        ran ahead of the model on a keyword guess. As a tool the model decides
        when the question actually warrants it, and can follow up on what comes
        back instead of being handed one fixed synthesis prompt.
        """
        from .search import scrape_sources, web_search

        topic = (args.get("topic") or "").strip()
        if not topic:
            return "Error: 'topic' is required."

        queries = [str(q).strip() for q in (args.get("queries") or []) if str(q).strip()]
        queries = queries[:4] or [topic]

        try:
            max_pages = int(args.get("max_pages", 15))
        except (TypeError, ValueError):
            max_pages = 15
        max_pages = max(5, min(max_pages, 50))

        results = await asyncio.gather(*(web_search(q, max_results=10) for q in queries))

        by_url: Dict[str, Dict[str, Any]] = {}
        for result in results:
            for source in result.sources:
                if source.get("url"):
                    by_url.setdefault(source["url"], source)

        blocks, usable = await scrape_sources(list(by_url.values())[:max_pages])
        if not usable:
            return json.dumps({
                "type": "deep_research",
                "text": (
                    f"Searched {len(queries)} quer{'y' if len(queries) == 1 else 'ies'} "
                    f"for '{topic}' but could not read any of the pages found. "
                    f"Try web_search with narrower terms."
                ),
                "queries": queries,
                "sources": [],
            })

        corpus = "\n\n".join(blocks)[:ToolExecutor.DEEP_RESEARCH_CHAR_BUDGET]
        return json.dumps({
            "type": "deep_research",
            "text": (
                f"Deep research on '{topic}'.\nQueries: {queries}\n"
                f"Pages read: {len(usable)}\n\n{corpus}"
            ),
            "queries": queries,
            "sources": usable,
        })

    @staticmethod
    async def _get_current_time(args: Dict, context: Dict) -> str:
        import datetime
        try:
            from django.utils import timezone
            current_time = timezone.now().strftime("%A, %B %d, %Y %I:%M %p %Z")
        except Exception:
            current_time = datetime.datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
        return json.dumps({"current_time": current_time})

    @staticmethod
    async def _read_url(args: Dict, context: Dict) -> str:
        import urllib.request
        url = args.get("url", "")
        if not url:
            return "Error: Missing URL"
        is_safe, ssrf_error = SSRFValidator.validate(url)
        if not is_safe:
            return json.dumps({"error": f"URL blocked: {ssrf_error}"})
        try:
            req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
            html = urllib.request.urlopen(req, timeout=10).read()
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html, 'html.parser')
                text = soup.get_text(separator=' ', strip=True)
            except ImportError:
                text = html.decode('utf-8', errors='ignore')
            return json.dumps({"url": url, "content": text[:READ_URL_CHAR_LIMIT]})
        except Exception as e:
            return json.dumps({"error": f"Failed to read URL '{url}': {str(e)}"})

    @staticmethod
    async def _read_attachment_text(args: Dict, context: Dict) -> str:
        from uuid import UUID
        from .models import ChatAttachment
        att_id = args.get("attachment_id")
        if not att_id:
            return "Error: Missing attachment_id"
        try:
            user_id = context.get("user_id")
            att = await ChatAttachment.objects.select_related('message__session').filter(
                id=UUID(att_id)
            ).afirst()
            if not att:
                return f"Error: Attachment with ID {att_id} not found."
            if user_id and att.message and att.message.session and att.message.session.user_id != user_id:
                return "Error: Access denied — attachment does not belong to your session."
            return json.dumps({
                "attachment_id": att_id,
                "filename": att.filename,
                "content": att.extracted_text
            })
        except Exception as e:
            return f"Error: Failed to read attachment from database: {str(e)}"

    @staticmethod
    async def _get_chat_message_full_text(args: Dict, context: Dict) -> str:
        from .models import ChatMessage
        msg_id = args.get("message_id")
        if not msg_id:
            return "Error: Missing message_id"
        try:
            user_id = context.get("user_id")
            msg = await ChatMessage.objects.select_related('session').filter(
                id=int(msg_id)
            ).afirst()
            if not msg:
                return f"Error: Message with ID {msg_id} not found."
            if user_id and msg.session and msg.session.user_id != user_id:
                return "Error: Access denied — message does not belong to your session."
            return json.dumps({"message_id": msg_id, "content": msg.content})
        except Exception as e:
            return f"Error: Failed to read message from database: {str(e)}"

    @staticmethod
    async def _search_conversation_history(args: Dict, context: Dict) -> str:
        """
        Grep-style lookup over everything this conversation has ever said or thought.

        Only the last HISTORY_WINDOW turns are replayed into the prompt; this is
        how the model reaches the rest without us paying for it every turn.

        Two design decisions worth stating, because both look like shortcuts:

        1. Term matching, not regex. The pattern comes from the model, and Python's
           `re` has no evaluation timeout, so a backtracking pattern like (a+)+$ run
           over stored message text pins a worker with no way to interrupt it. The
           retrieval quality difference is small; the availability difference is not.

        2. Scanning in Python rather than filtering in the DB. Half of what we
           search is metadata['thinking'], and JSON-key containment lookups differ
           between Postgres and the SQLite used by the test suite. A bounded scan
           behaves identically on both. HISTORY_SEARCH_SCAN_LIMIT keeps it cheap.
        """
        from asgiref.sync import sync_to_async
        from .models import ChatMessage

        query = (args.get("query") or "").strip()
        if not query:
            return json.dumps({"error": "Missing query."})
        if len(query) > HISTORY_SEARCH_MAX_PATTERN_LEN:
            query = query[:HISTORY_SEARCH_MAX_PATTERN_LEN]

        scope = (args.get("scope") or "all").lower()
        if scope not in ("all", "messages", "reasoning"):
            scope = "all"
        role = (args.get("role") or "any").lower()
        if role not in ("any", "user", "assistant"):
            role = "any"

        session_id = context.get("session_id")
        user_id = context.get("user_id")
        if not session_id:
            return json.dumps({"error": "No conversation context available for search."})

        terms = [t for t in re.split(r'\s+', query.lower()) if len(t) > 1]
        if not terms:
            return json.dumps({"error": "Query too short to search."})

        def _search() -> list[dict]:
            qs = ChatMessage.objects.filter(session_id=session_id)
            # Ownership: the session must belong to the caller. Scoping by
            # session_id alone would let a leaked/guessed UUID read another
            # user's conversation.
            if user_id:
                qs = qs.filter(session__user_id=user_id)
            if role != "any":
                qs = qs.filter(role=role)
            else:
                qs = qs.filter(role__in=["user", "assistant"])

            rows = list(
                qs.order_by('-created_at')
                .values('id', 'role', 'content', 'metadata', 'created_at')[:HISTORY_SEARCH_SCAN_LIMIT]
            )

            scored = []
            for r in rows:
                haystacks = []
                if scope in ("all", "messages"):
                    haystacks.append(("message", r.get('content') or ""))
                if scope in ("all", "reasoning"):
                    meta = r.get('metadata') or {}
                    if isinstance(meta, dict):
                        thinking = meta.get('thinking') or ""
                        if thinking:
                            haystacks.append(("reasoning", thinking))

                best = None
                for kind, text in haystacks:
                    low = text.lower()
                    hits = [t for t in terms if t in low]
                    if not hits:
                        continue
                    # Rank by how much of the query a row accounts for; ties go to
                    # the row that matched on what was actually said over what was
                    # merely thought.
                    score = len(hits) / len(terms) + (0.1 if kind == "message" else 0)
                    if best is None or score > best[0]:
                        first = min(low.find(t) for t in hits)
                        best = (score, kind, text, first)

                if best:
                    score, kind, text, pos = best
                    half = HISTORY_SEARCH_SNIPPET_CHARS // 2
                    start = max(0, pos - half)
                    end = min(len(text), pos + half)
                    snippet = text[start:end]
                    if start > 0:
                        snippet = "..." + snippet
                    if end < len(text):
                        snippet = snippet + "..."
                    scored.append({
                        "score": round(score, 3),
                        "message_id": r['id'],
                        "role": r['role'],
                        "found_in": kind,
                        "timestamp": r['created_at'].isoformat(),
                        "snippet": snippet,
                    })

            scored.sort(key=lambda x: (-x["score"], -x["message_id"]))
            return scored[:HISTORY_SEARCH_MAX_MATCHES]

        try:
            matches = await sync_to_async(_search)()
        except Exception as e:
            logger.error(f"search_conversation_history failed: {e}")
            return json.dumps({"error": f"History search failed: {e}"})

        if not matches:
            return json.dumps({
                "matches": [],
                "message": (
                    "No earlier messages matched those terms. Try fewer or different "
                    "keywords before concluding the information was never provided."
                ),
            })

        # Final ceiling. The whole point of this tool is to keep the context small,
        # so it must not be able to return more than the window it is protecting —
        # drop whole matches rather than truncating mid-snippet.
        payload, total = [], 0
        for m in matches:
            cost = len(m["snippet"])
            if total + cost > HISTORY_SEARCH_MAX_TOTAL_CHARS:
                break
            payload.append(m)
            total += cost

        return json.dumps({
            "matches": payload,
            "returned": len(payload),
            "truncated": len(payload) < len(matches),
            "hint": "Call get_chat_message_full_text(message_id=...) for the full text of any match.",
        })

    @staticmethod
    async def _render_html_artifact(args: Dict, context: Dict) -> str:
        """
        Hand a self-contained HTML snippet to the client for sandboxed rendering.

        Nothing is executed here. The server's job is to bound the payload and
        pin the dimensions; the frontend renders it in a sandboxed iframe.

        Clamping server-side matters even though the frontend clamps too: the
        stored metadata is replayed when the conversation is reloaded, so an
        unclamped size persisted today becomes an oversized card on every future
        render. Bound it once, at write time.
        """
        html = args.get("html") or ""
        if not html.strip():
            return json.dumps({"error": "Missing html."})

        truncated = False
        if len(html) > HTML_ARTIFACT_MAX_CHARS:
            html = html[:HTML_ARTIFACT_MAX_CHARS]
            truncated = True

        def _clamp(raw, default, lo, hi) -> int:
            try:
                v = int(raw)
            except (TypeError, ValueError):
                return default
            return max(lo, min(hi, v))

        width = _clamp(args.get("width"), HTML_ARTIFACT_DEFAULT_WIDTH,
                       HTML_ARTIFACT_MIN_WIDTH, HTML_ARTIFACT_MAX_WIDTH)
        height = _clamp(args.get("height"), HTML_ARTIFACT_DEFAULT_HEIGHT,
                        HTML_ARTIFACT_MIN_HEIGHT, HTML_ARTIFACT_MAX_HEIGHT)

        title = (args.get("title") or "Rendered output").strip()[:80]

        return json.dumps({
            "type": "html_artifact",
            "title": title,
            "html": html,
            "width": width,
            "height": height,
            "truncated": truncated,
            "note": (
                f"Rendered at {width}x{height}px in a sandboxed frame."
                + (" Content was truncated to fit the size limit." if truncated else "")
            ),
        })

    @staticmethod
    async def _list_knowledge_bases(args: Dict, context: Dict) -> str:
        from asgiref.sync import sync_to_async
        from inference.models import KnowledgeBase
        user_id = context.get("user_id")
        if not user_id:
            return json.dumps({"error": "No user context."})
        try:
            def _list():
                kbs = KnowledgeBase.objects.filter(user_id=user_id).values(
                    'id', 'name', 'description', 'doc_count', 'vector_count',
                    'index_size_bytes', 'is_default', 'embedding_model',
                )
                return list(kbs)

            kbs = await sync_to_async(_list)()
            for kb in kbs:
                b = kb['index_size_bytes']
                for unit in ('B', 'KB', 'MB', 'GB'):
                    if b < 1024:
                        kb['size_human'] = f'{b:.1f} {unit}'
                        break
                    b /= 1024
                else:
                    kb['size_human'] = f'{b:.1f} TB'
            return json.dumps({"knowledge_bases": kbs, "count": len(kbs)})
        except Exception as e:
            return json.dumps({"error": f"Failed to list KBs: {e}"})

    @staticmethod
    async def _knowledge_base_search(args: Dict, context: Dict) -> str:
        from asgiref.sync import sync_to_async
        from inference.engine import get_hnsw_kb, get_kb_for_user
        query = args.get("query", "")
        if not query:
            return "Error: Missing search query"
        top_k = min(int(args.get("top_k", 5)), 20)
        kb_id = args.get("kb_id")
        user_id = context.get("user_id")
        if not user_id:
            return json.dumps({"error": "No user context for knowledge base search."})
        try:
            if kb_id:
                from inference.models import KnowledgeBase
                kb_model = await sync_to_async(
                    lambda: KnowledgeBase.objects.filter(id=kb_id, user_id=user_id).first()
                )()
                if not kb_model:
                    return json.dumps({"error": f"KB {kb_id} not found or not owned by user."})
                hnsw = get_hnsw_kb(kb_model.id, kb_model.s3_index_key or f'indices/kb_{kb_model.id}')
                await hnsw.initialize()
            else:
                _, hnsw = await get_kb_for_user(user_id)

            results = await hnsw.search(query, top_k=top_k)
            if not results:
                return json.dumps({"status": "no_results", "message": "No relevant documents found. Try a different query or check that documents are indexed."})

            items = [
                {
                    "document_id": r.document_id,
                    "score": round(r.score, 4),
                    "content": r.content[:2000],
                    "metadata": r.metadata,
                    "is_image": r.is_image,
                }
                for r in results
            ]
            return json.dumps({"status": "success", "results": items, "count": len(items)})
        except Exception as e:
            return f"Error: Knowledge base search failed: {str(e)}"

    @staticmethod
    async def _scrape_webpage(args: Dict, context: Dict) -> str:
        import urllib.request
        url = args.get("url", "")
        if not url:
            return "Error: Missing URL"
        is_safe, ssrf_error = SSRFValidator.validate(url)
        if not is_safe:
            return json.dumps({"error": f"URL blocked: {ssrf_error}"})
        extract_types = args.get("extract", ["text", "headings", "links", "tables", "metadata", "images"])
        try:
            def _scrape():
                req = urllib.request.Request(url, headers={
                    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
                })
                return urllib.request.urlopen(req, timeout=15).read()

            html_bytes = await asyncio.to_thread(_scrape)
            try:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(html_bytes, 'html.parser')
            except ImportError:
                text = html_bytes.decode('utf-8', errors='ignore')[:READ_URL_CHAR_LIMIT]
                return json.dumps({"url": url, "text": text, "error": "BeautifulSoup not installed, returning raw text"})

            result = {"url": url}

            if "metadata" in extract_types:
                result["metadata"] = {
                    "title": soup.title.string.strip() if soup.title and soup.title.string else "",
                    "description": "",
                    "og_image": "",
                }
                meta_desc = soup.find("meta", attrs={"name": "description"})
                if meta_desc:
                    result["metadata"]["description"] = meta_desc.get("content", "")[:500]
                og_img = soup.find("meta", attrs={"property": "og:image"})
                if og_img:
                    result["metadata"]["og_image"] = og_img.get("content", "")

            if "headings" in extract_types:
                headings = []
                for level in range(1, 7):
                    for h in soup.find_all(f"h{level}"):
                        text = h.get_text(strip=True)
                        if text:
                            headings.append({"level": level, "text": text[:200]})
                result["headings"] = headings[:50]

            if "links" in extract_types:
                links = []
                for a in soup.find_all("a", href=True):
                    href = a["href"]
                    link_text = a.get_text(strip=True)[:100]
                    if href and not href.startswith(("#", "javascript:")):
                        links.append({"text": link_text, "href": href})
                result["links"] = links[:100]

            if "tables" in extract_types:
                tables = []
                for table in soup.find_all("table")[:5]:
                    rows = []
                    for tr in table.find_all("tr")[:30]:
                        cells = [td.get_text(strip=True)[:200] for td in tr.find_all(["td", "th"])]
                        if cells:
                            rows.append(cells)
                    if rows:
                        tables.append(rows)
                result["tables"] = tables

            if "images" in extract_types:
                images = []
                for img in soup.find_all("img", src=True)[:20]:
                    src = img.get("src", "")
                    alt = img.get("alt", "")[:100]
                    if src:
                        images.append({"src": src, "alt": alt})
                result["images"] = images

            if "text" in extract_types:
                for tag in soup(["script", "style", "nav", "footer", "header"]):
                    tag.decompose()
                text = soup.get_text(separator="\n", strip=True)
                result["text"] = text[:READ_URL_CHAR_LIMIT]

            return json.dumps({"status": "success", **result})
        except Exception as e:
            return json.dumps({"status": "error", "error": f"Failed to scrape '{url}': {str(e)}"})

    @staticmethod
    async def _frontend_action(func_name: str, args: Dict, context: Dict) -> str:
        from channels.layers import get_channel_layer
        user_id = context.get("user_id")
        if not user_id:
            return "Error: Missing user context. Cannot interact with frontend."
        try:
            channel_layer = get_channel_layer()
            if not channel_layer:
                return "Error: Channel layer is not configured."
            await channel_layer.group_send(
                f"buddy_{user_id}",
                {
                    "type": "trigger_action",
                    "action": func_name,
                    "parameters": args,
                }
            )
            return json.dumps({
                "status": "success",
                "message": f"Action '{func_name}' sent to the frontend successfully."
            })
        except Exception as e:
            return f"Error: Failed to execute {func_name}: {str(e)}"

    @staticmethod
    async def _dispatch_ui_actions(args: Dict, context: Dict) -> str:
        from channels.layers import get_channel_layer
        user_id = context.get("user_id")
        if not user_id:
            return "Error: Missing user context."
        try:
            channel_layer = get_channel_layer()
            if not channel_layer:
                return "Error: Channel layer is not configured."
            
            actions = args.get("actions", [])
            if not actions:
                return "Error: No actions provided."

            await channel_layer.group_send(
                f"buddy_{user_id}",
                {
                    "type": "trigger_multiple_actions",
                    "actions": actions,
                }
            )
            return json.dumps({
                "status": "success",
                "message": f"Dispatched {len(actions)} UI actions successfully."
            })
        except Exception as e:
            return f"Error: Failed to dispatch UI actions: {str(e)}"

    @staticmethod
    async def _call_internal_api(args: Dict, context: Dict) -> str:
        from asgiref.sync import sync_to_async
        from django.urls import resolve, Resolver404
        from rest_framework.test import APIRequestFactory
        from django.contrib.auth import get_user_model
        import json

        User = get_user_model()
        user_id = context.get("user_id")
        if not user_id:
            return json.dumps({"error": "No user_id found in context"})

        method = args.get("method", "GET").upper()
        path = args.get("path", "")
        data = args.get("data", {})
        query_params = args.get("query_params", {})

        if not path:
            return json.dumps({"error": "Path is required"})
            
        if not path.startswith("/"):
            path = "/" + path
            
        def _execute_request():
            try:
                user = User.objects.get(id=user_id)
            except User.DoesNotExist:
                return {"error": f"User {user_id} not found"}

            try:
                match = resolve(path)
            except Resolver404:
                return {"error": f"Endpoint not found: {path}", "status": 404}

            factory = APIRequestFactory()
            
            # Build full path with query params if any
            full_path = path
            if query_params:
                from urllib.parse import urlencode
                full_path = f"{path}?{urlencode(query_params)}"

            if method == "GET":
                request = factory.get(full_path)
            elif method == "POST":
                request = factory.post(full_path, data, format='json')
            elif method == "PUT":
                request = factory.put(full_path, data, format='json')
            elif method == "PATCH":
                request = factory.patch(full_path, data, format='json')
            elif method == "DELETE":
                request = factory.delete(full_path)
            else:
                return {"error": f"Unsupported method: {method}"}

            request.user = user

            try:
                # Need to manually apply DRF's authentication wrapper if force_authenticate isn't used directly on the view
                # Since we're calling the view directly, we pass the request object
                response = match.func(request, *match.args, **match.kwargs)
                
                # Check if it has a render method (DRF Response)
                if hasattr(response, 'render'):
                    response.render()
                    
                try:
                    # Attempt to parse as JSON first
                    content = json.loads(response.content.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError):
                    # Fallback to string if not JSON
                    content = response.content.decode('utf-8', errors='replace')
                    
                return {
                    "status_code": response.status_code,
                    "data": content
                }
            except Exception as e:
                logger.exception(f"Internal API error on {method} {path}: {e}")
                return {"error": f"Internal server error: {str(e)}", "status": 500}

        # Run synchronously to avoid breaking Django ORM limits in async context
        result = await sync_to_async(_execute_request)()
        return json.dumps(result)

    @classmethod
    async def execute(cls, func_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Execute a tool dynamically and return the string response."""
        try:
            from mcp_integration.tool_provider import is_mcp_tool, MCPToolProvider
            if is_mcp_tool(func_name):
                return await MCPToolProvider.execute(func_name, args, context.get("user_id"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"MCP dispatch failed for {func_name}: {e}")
            return f"Error executing MCP tool {func_name}: {str(e)}"

        try:
            dispatch = {
                "web_search": cls._web_search,
                "deep_research": cls._deep_research,
                "image_search": cls._image_search,
                "video_search": cls._video_search,
                "get_current_time": cls._get_current_time,
                "dispatch_ui_actions": cls._dispatch_ui_actions,
                "call_internal_api": cls._call_internal_api,
                "read_url": cls._read_url,
                "read_attachment_text": cls._read_attachment_text,
                "get_chat_message_full_text": cls._get_chat_message_full_text,
                "search_conversation_history": cls._search_conversation_history,
                "render_html_artifact": cls._render_html_artifact,
                "list_knowledge_bases": cls._list_knowledge_bases,
                "knowledge_base_search": cls._knowledge_base_search,
                "scrape_webpage": cls._scrape_webpage,
            }

            if func_name in dispatch:
                return await dispatch[func_name](args, context)

            if func_name in ("frontend_click", "frontend_fill", "frontend_navigate"):
                return await cls._frontend_action(func_name, args, context)

            return f"Error: Tool '{func_name}' is not recognized."

        except Exception as e:
            logger.error(f"Error executing tool {func_name}: {e}")
            return f"Error executing tool {func_name}: {str(e)}"


# Module-level aliases for backward compatibility
AVAILABLE_TOOLS = ToolExecutor.AVAILABLE_TOOLS
validate_url_for_ssrf = SSRFValidator.validate
get_available_tools = ToolExecutor.get_available_tools
execute_tool = ToolExecutor.execute
