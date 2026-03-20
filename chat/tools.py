"""
Shared Tool Registry for Agentic Execution
"""
import logging
from typing import Any, Dict, List
from workflow_backend.thresholds import READ_URL_CHAR_LIMIT

logger = logging.getLogger(__name__)

# Define the tools available to the LLM
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
                "required": [],
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
    }
]

async def execute_tool(func_name: str, args: Dict[str, Any], context: Dict[str, Any]) -> str:
    """Execute a tool dynamically and return the string response."""
    try:
        if func_name == "web_search":
            from .views import perform_web_search
            query = args.get("query", "")
            if not query:
                return "Error: Missing search query"
            res = await perform_web_search(query)
            
            # Since tools currently return strings, we'll embed the sources in JSON
            import json
            return json.dumps({
                "type": "search_results",
                "text": f"Search Results for '{query}':\n\n{res['results_text']}",
                "sources": res.get("sources", [])
            })
            
        elif func_name == "image_search":
            from .views import perform_image_search
            query = args.get("query", "")
            if not query:
                return "Error: Missing image search query"
            res = await perform_image_search(query)
            import json
            # DO NOT return the actual array of URLs into the agent's context window.
            # We return a simple confirmation message. The `chat/views.py` stream handler 
            # will intercept `res["images"]` and pipe it directly to the UI independently.
            return json.dumps({
                "type": "image_results",
                "text": f"Successfully retrieved {len(res.get('images', []))} images for '{query}'. They have been attached to the UI view.",
                "images": res.get("images", []) # The UI interceptor strips this out before passing to LLM context
            })
            
        elif func_name == "video_search":
            from .views import perform_video_search
            query = args.get("query", "")
            if not query:
                return "Error: Missing video search query"
            res = await perform_video_search(query)
            import json
            return json.dumps({
                "type": "video_results",
                "text": f"Successfully retrieved {len(res.get('videos', []))} videos for '{query}'. They have been attached to the UI view.",
                "videos": res.get("videos", []) 
            })
            
        elif func_name == "suggest_workflow":
            from .views import suggest_workflow
            intent = args.get("intent", "")
            user_id = context.get("user_id")
            if not intent or not user_id:
                return "Error: Missing intent or missing user context to search workflows"
            
            sug = await suggest_workflow(user_id, intent)
            if not sug:
                import json
                return json.dumps({"found": False, "message": "No relevant workflows found in your account."})
                
            import json
            return json.dumps({
                "found": True, 
                "workflow_id": sug["workflow_id"],
                "name": sug["name"],
                "description": sug.get("description", "No description provided")
            })
            
        elif func_name == "get_current_time":
            import datetime
            try:
                from django.utils import timezone
                current_time = timezone.now().strftime("%A, %B %d, %Y %I:%M %p %Z")
            except Exception:
                current_time = datetime.datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
            import json
            return json.dumps({"current_time": current_time})
            
        elif func_name == "read_url":
            url = args.get("url", "")
            if not url:
                return "Error: Missing URL"
            try:
                import urllib.request
                req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'})
                html = urllib.request.urlopen(req, timeout=10).read()
                try:
                    from bs4 import BeautifulSoup
                    soup = BeautifulSoup(html, 'html.parser')
                    text = soup.get_text(separator=' ', strip=True)
                except ImportError:
                    text = html.decode('utf-8', errors='ignore')
                import json
                return json.dumps({"url": url, "content": text[:READ_URL_CHAR_LIMIT]})
            except Exception as e:
                import json
                return json.dumps({"error": f"Failed to read URL '{url}': {str(e)}"})
            
        elif func_name == "read_attachment_text":
            att_id = args.get("attachment_id")
            if not att_id:
                return "Error: Missing attachment_id"
            try:
                from .models import ChatAttachment
                from uuid import UUID
                att = await ChatAttachment.objects.filter(id=UUID(att_id)).afirst()
                if not att:
                    return f"Error: Attachment with ID {att_id} not found."
                import json
                return json.dumps({
                    "attachment_id": att_id,
                    "filename": att.filename,
                    "content": att.extracted_text
                })
            except Exception as e:
                return f"Error: Failed to read attachment from database: {str(e)}"
            
        elif func_name == "get_chat_message_full_text":
            msg_id = args.get("message_id")
            if not msg_id:
                return "Error: Missing message_id"
            try:
                from .models import ChatMessage
                msg = await ChatMessage.objects.filter(id=int(msg_id)).afirst()
                if not msg:
                    return f"Error: Message with ID {msg_id} not found."
                import json
                return json.dumps({
                    "message_id": msg_id,
                    "content": msg.content
                })
            except Exception as e:
                return f"Error: Failed to read message from database: {str(e)}"
            
        else:
            return f"Error: Tool '{func_name}' is not recognized."
            
    except Exception as e:
        logger.error(f"Error executing tool {func_name}: {e}")
        return f"Error executing tool {func_name}: {str(e)}"
