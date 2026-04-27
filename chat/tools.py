"""
Shared Tool Registry for Agentic Execution
"""
import ipaddress
import json
import logging
import socket
from typing import Any, Dict, List
from django.db.models import Q as models_Q
from urllib.parse import urlparse
from workflow_backend.thresholds import READ_URL_CHAR_LIMIT

logger = logging.getLogger(__name__)


class SSRFValidator:
    _BLOCKED_IP_RANGES = [
        ipaddress.ip_network('10.0.0.0/8'),
        ipaddress.ip_network('172.16.0.0/12'),
        ipaddress.ip_network('192.168.0.0/16'),
        ipaddress.ip_network('127.0.0.0/8'),
        ipaddress.ip_network('169.254.0.0/16'),  # AWS/GCP metadata
        ipaddress.ip_network('::1/128'),
        ipaddress.ip_network('fc00::/7'),
        ipaddress.ip_network('fe80::/10'),
        ipaddress.ip_network('0.0.0.0/8'),
    ]

    _BLOCKED_HOSTNAMES = {
        'metadata.google.internal',
        'metadata.google',
        'kubernetes.default',
        'kubernetes.default.svc',
    }

    @classmethod
    def validate(cls, url: str) -> tuple[bool, str]:
        """
        Validate a URL to prevent SSRF attacks.
        Returns (is_safe, error_message).
        """
        try:
            parsed = urlparse(url)
        except Exception:
            return False, "Invalid URL format"

        if parsed.scheme not in ('http', 'https'):
            return False, f"URL scheme '{parsed.scheme}' is not allowed. Only http/https permitted."

        hostname = parsed.hostname
        if not hostname:
            return False, "URL has no hostname"

        if hostname.lower() in cls._BLOCKED_HOSTNAMES:
            return False, f"Access to '{hostname}' is blocked"

        try:
            resolved_ips = socket.getaddrinfo(hostname, parsed.port or 80, proto=socket.IPPROTO_TCP)
            for family, _type, _proto, _canonname, sockaddr in resolved_ips:
                ip = ipaddress.ip_address(sockaddr[0])
                for blocked_range in cls._BLOCKED_IP_RANGES:
                    if ip in blocked_range:
                        return False, "Access to internal/private network addresses is blocked"
        except socket.gaierror:
            return False, f"Could not resolve hostname '{hostname}'"

        return True, ""


class ToolExecutor:
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
                "name": "suggest_workflow",
                "description": "Search the user's available platform workflows to suggest one that solves their request. Run this when the user asks to build, run, or find a workflow, automation, or sequence.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "intent": {
                            "type": "string",
                            "description": "A description of what the user wants the workflow to accomplish (e.g. 'send an email to my boss', 'sync data to salesforce')."
                        }
                    },
                    "required": ["intent"],
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
                "name": "execute_python_code",
                "description": "Execute python code in a secure sandbox. Use this to perform calculations, data transformation, or execute logic. Print your results to standard output so they can be captured.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "code": {
                            "type": "string",
                            "description": "The python code string to execute."
                        },
                        "engine": {
                            "type": "string",
                            "enum": ["in_process", "wasm"],
                            "description": "The sandbox engine. 'in_process' is fastest but uses AST limits. 'wasm' enforces strict CPU/RAM limits via WebAssembly (good for untrusted logic)."
                        }
                    },
                    "required": ["code"],
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
                "name": "list_workflows",
                "description": "List all workflows available in the user's account. Use this to show the user their existing automations or to find a workflow before running it.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "search": {
                            "type": "string",
                            "description": "Optional search term to filter workflows by name or description."
                        }
                    },
                    "required": [],
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "run_workflow",
                "description": "Trigger the execution of a user's workflow by its ID. Use after finding a workflow via list_workflows or suggest_workflow.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "workflow_id": {
                            "type": "integer",
                            "description": "The ID of the workflow to execute."
                        },
                        "input_data": {
                            "type": "object",
                            "description": "Optional input parameters to pass to the workflow."
                        }
                    },
                    "required": ["workflow_id"],
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

    @staticmethod
    async def get_available_tools(user_id: int | None) -> List[Dict[str, Any]]:
        """
        Return the full tool list for this user: built-in tools + any MCP tools
        the user has enabled. Safe to call on every agent turn (MCP tool lists
        are cached in Redis).
        """
        tools = list(ToolExecutor.AVAILABLE_TOOLS)
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
        from .views import perform_web_search
        query = args.get("query", "")
        if not query:
            return "Error: Missing search query"
        res = await perform_web_search(query)
        return json.dumps({
            "type": "search_results",
            "text": f"Search Results for '{query}':\n\n{res['results_text']}",
            "sources": res.get("sources", [])
        })

    @staticmethod
    async def _image_search(args: Dict, context: Dict) -> str:
        from .views import perform_image_search
        query = args.get("query", "")
        if not query:
            return "Error: Missing image search query"
        res = await perform_image_search(query)
        return json.dumps({
            "type": "image_results",
            "text": f"Successfully retrieved {len(res.get('images', []))} images for '{query}'. They have been attached to the UI view.",
            "images": res.get("images", [])
        })

    @staticmethod
    async def _video_search(args: Dict, context: Dict) -> str:
        from .views import perform_video_search
        query = args.get("query", "")
        if not query:
            return "Error: Missing video search query"
        res = await perform_video_search(query)
        return json.dumps({
            "type": "video_results",
            "text": f"Successfully retrieved {len(res.get('videos', []))} videos for '{query}'. They have been attached to the UI view.",
            "videos": res.get("videos", [])
        })

    @staticmethod
    async def _suggest_workflow(args: Dict, context: Dict) -> str:
        from .views import suggest_workflow
        intent = args.get("intent", "")
        user_id = context.get("user_id")
        if not intent or not user_id:
            return "Error: Missing intent or missing user context to search workflows"
        sug = await suggest_workflow(user_id, intent)
        if not sug:
            return json.dumps({"found": False, "message": "No relevant workflows found in your account."})
        return json.dumps({
            "found": True,
            "workflow_id": sug["workflow_id"],
            "name": sug["name"],
            "description": sug.get("description", "No description provided")
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
    async def _execute_python_code(args: Dict, context: Dict) -> str:
        import asyncio
        from executor.sandbox.safe_execution import safe_execute
        code = args.get("code", "")
        engine = args.get("engine", "in_process")
        if not code:
            return "Error: Missing code"
        try:
            exec_res = await asyncio.to_thread(safe_execute, code, None, engine)
            if not exec_res.get("success"):
                return json.dumps({
                    "status": "error",
                    "error": exec_res.get("error") or "Execution failed with no error message.",
                    "stderr": exec_res.get("stderr") or ""
                })
            return json.dumps({
                "status": "success",
                "output": exec_res.get("output"),
                "result": str(exec_res.get("result")) if exec_res.get("result") is not None else None
            })
        except Exception as e:
            return f"Error: Sandbox execution failed: {str(e)}"

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
        import asyncio
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
    async def _list_workflows(args: Dict, context: Dict) -> str:
        from asgiref.sync import sync_to_async
        from orchestrator.models import Workflow
        user_id = context.get("user_id")
        if not user_id:
            return "Error: Missing user context"
        search_term = args.get("search", "")
        try:
            qs = Workflow.objects.filter(user_id=user_id)
            if search_term:
                qs = qs.filter(
                    models_Q(name__icontains=search_term) | models_Q(description__icontains=search_term)
                )
            workflows = await sync_to_async(list)(qs.values('id', 'name', 'description', 'is_active')[:50])
            return json.dumps({"status": "success", "workflows": workflows, "count": len(workflows)})
        except Exception as e:
            return f"Error: Failed to list workflows: {str(e)}"

    @staticmethod
    async def _run_workflow(args: Dict, context: Dict) -> str:
        from asgiref.sync import sync_to_async
        from orchestrator.models import Workflow
        workflow_id = args.get("workflow_id")
        if not workflow_id:
            return "Error: Missing workflow_id"
        user_id = context.get("user_id")
        input_data = args.get("input_data", {})
        try:
            wf = await sync_to_async(Workflow.objects.filter(id=int(workflow_id), user_id=user_id).first)()
            if not wf:
                return json.dumps({"status": "error", "error": f"Workflow {workflow_id} not found or access denied."})
            try:
                from compiler.engine import WorkflowEngine
                engine = WorkflowEngine()
                result = await engine.execute(workflow_id=wf.id, user_id=user_id, input_data=input_data)
                return json.dumps({
                    "status": "success",
                    "workflow_id": wf.id,
                    "workflow_name": wf.name,
                    "execution_result": str(result)[:3000],
                })
            except ImportError:
                return json.dumps({
                    "status": "queued",
                    "workflow_id": wf.id,
                    "workflow_name": wf.name,
                    "message": "Workflow execution has been queued. The workflow engine will process it.",
                })
        except Exception as e:
            return f"Error: Failed to run workflow: {str(e)}"

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
                "image_search": cls._image_search,
                "video_search": cls._video_search,
                "suggest_workflow": cls._suggest_workflow,
                "get_current_time": cls._get_current_time,
                "dispatch_ui_actions": cls._dispatch_ui_actions,
                "call_internal_api": cls._call_internal_api,
                "read_url": cls._read_url,
                "read_attachment_text": cls._read_attachment_text,
                "get_chat_message_full_text": cls._get_chat_message_full_text,
                "execute_python_code": cls._execute_python_code,
                "list_knowledge_bases": cls._list_knowledge_bases,
                "knowledge_base_search": cls._knowledge_base_search,
                "scrape_webpage": cls._scrape_webpage,
                "list_workflows": cls._list_workflows,
                "run_workflow": cls._run_workflow,
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
w,
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
