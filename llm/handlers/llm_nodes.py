"""
Local Ollama inference, plus the skill/attachment helpers shared with the
OpenAI-protocol providers.

Ollama is the one supported provider that does not speak the OpenAI
chat-completions protocol — it posts to `/api/chat` and streams bare JSON
objects rather than SSE `choices[].delta` frames — so it keeps its own
transport here while `openai_compatible.py` serves the other three.
"""
import json
import logging
import httpx
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeExecutionResult,
    build_json_schema_from_fields,
    format_schema_for_prompt,
)
from .openai_compatible import validate_attachment_path
from .llm_base import ReasoningSplitter

logger = logging.getLogger(__name__)



#: Attachment path validation is shared with the OpenAI-protocol handlers so a
#: fix to the traversal check reaches every provider at once.
_validate_attachment_path = validate_attachment_path

def format_skills_as_context(skills: list[dict]) -> str:
    """Format skill list into a context block for LLM prompts."""
    if not skills:
        return ""
    
    parts = ["\n[CONTEXT / SKILLS]"]
    for s in skills:
        parts.append(f"### {s['title']}\n{s['content']}")
    parts.append("[END CONTEXT]\n")
    return "\n".join(parts)


# Detect if a model is specifically for image generation.
def is_image_generation_model(model: str) -> bool:
    image_keywords = ['dall-e', 'midjourney', 'stable-diffusion', 'flux', 'imagen', 'recraft', 'leonardo', 'stable-image']
    return any(keyword in model.lower() for keyword in image_keywords)

# Detect if a model is specifically for video generation.
def is_video_generation_model(model: str) -> bool:
    video_keywords = [
        'veo', 'sora', 'kling', 'luma', 'dream-machine', 'runway', 
        'gen-3', 'gen-2', 'pika', 'haiper', 'mochi', 'cogvideo', 'ltx'
    ]
    return any(keyword in model.lower() for keyword in video_keywords)


async def resolve_node_skills(config: dict[str, Any]) -> list[dict]:
    """
    Resolve this call's skill IDs from config into `{title, content}` dicts.

    Deduplicates by title and skips ids that no longer resolve, so a deleted
    skill degrades to a warning rather than a failed call.

    This used to also merge `context.skills`, which was `[]` on every call ever
    made — the field was only ever populated by the DAG runtime. That is why
    `context` is no longer a parameter.
    """
    skill_ids = config.get('skills', [])
    if not skill_ids or not isinstance(skill_ids, list):
        return []

    valid_ids = []
    for sid in skill_ids:
        try:
            valid_ids.append(int(sid))
        except (ValueError, TypeError):
            logger.warning(f"Invalid skill ID: {sid}")
    if not valid_ids:
        return []

    skills: list[dict] = []
    seen_titles: set[str] = set()
    found_ids: set[int] = set()
    try:
        from skills.models import Skill
        async for skill in Skill.objects.filter(id__in=valid_ids):
            found_ids.add(skill.id)
            if skill.title not in seen_titles:
                skills.append({'title': skill.title, 'content': skill.content})
                seen_titles.add(skill.title)
    except Exception as e:
        logger.error(f"Failed to resolve skills: {e}")
        return skills

    if missing := set(valid_ids) - found_ids:
        logger.warning(f"Skills not found (may have been deleted): {missing}")
    return skills


if TYPE_CHECKING:
    from llm.context import ExecutionContext



def _build_ollama_messages(config: dict[str, Any], prompt: str,
                           skills: list[dict]) -> list[dict[str, Any]]:
    """The chat `messages` array for an Ollama request.

    Shared by `execute` and `stream_execute`. It used to be inlined in both,
    ~90 near-identical lines each, and the two copies had already drifted: one
    logged a skipped non-image attachment and the other dropped it silently,
    and they caught different exception sets around the same `open()`. A
    request built two ways is a request that can be right one way.

    Attachments are best-effort by design — one unreadable file must not fail
    a whole request — but never silent, because a dropped attachment looks to
    the user exactly like a model that ignored what they sent.
    """
    import base64

    messages: list[dict[str, Any]] = []

    system_message = config.get("system_message", "")
    if system_message or skills:
        messages.append({
            "role": "system",
            "content": f"{system_message}{format_skills_as_context(skills)}",
        })

    history = config.get("history", [])
    if history:
        messages.extend(history)

    parts: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
    for att in config.get("attachments", []) or []:
        try:
            # Ollama's multimodal models take images only.
            if att.file_type != 'image':
                logger.info("Skipping unsupported attachment type %s for Ollama",
                            att.file_type)
                continue
            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
            if not _validate_attachment_path(file_path):
                logger.warning("Blocked path traversal in Ollama attachment")
                continue
            with open(file_path, "rb") as handle:
                parts.append({
                    "type": "image",
                    "image": base64.b64encode(handle.read()).decode('utf-8'),
                })
        except Exception as exc:  # noqa: BLE001 - one bad file is not a failed request
            logger.warning("Skipping unreadable Ollama attachment: %s", exc)

    # A lone text part goes as a plain string: that is the shape Ollama's
    # text-only models expect, and the multipart form is only for images.
    messages.append({
        "role": "user",
        "content": parts if len(parts) > 1 else prompt,
    })
    return messages


class OllamaNode(BaseNodeHandler):
    """
    Call local Ollama instance for inference.
    
    No API key required - runs on localhost.
    """
    
    node_type = "ollama"

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        model = config.get("model", "llama3.2")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "")
        base_url = config.get("base_url", "http://localhost:11434")
        temperature = config.get("temperature", 0.7)
        show_thinking = config.get("thinking", False)
        
        effective_prompt = prompt
        is_native_reasoner = any(m in model.lower() for m in ["r1", "reasoning", "thought"])
        if show_thinking and not is_native_reasoner:
            effective_prompt += "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."

        try:
            all_skills = await resolve_node_skills(config)
            async with httpx.AsyncClient(timeout=300) as client:
                messages = _build_ollama_messages(config, effective_prompt, all_skills)

                req_payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {"temperature": temperature},
                }

                splitter = ReasoningSplitter()
                async with client.stream(
                    "POST", 
                    f"{base_url.rstrip('/')}/api/chat",
                    json=req_payload,
                    timeout=None
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"Ollama error: {response.status_code}"}
                        return

                    async for line in response.aiter_lines():
                        if not line: continue
                        try:
                            chunk = json.loads(line)
                            msg = chunk.get("message", {})
                            text = msg.get("content", "")
                            
                            if text:
                                if "reasoning_content" in msg and msg["reasoning_content"]:
                                     yield {"type": "thinking", "content": msg["reasoning_content"]}

                                events = splitter.feed(text)
                                for kind, chunk_text in events:
                                    yield {"type": kind, "content": chunk_text}
                            
                            if chunk.get("done"):
                                yield {
                                    "type": "metadata",
                                    "usage": {
                                        # Map Ollama metrics to standard keys
                                        "total_duration": chunk.get("total_duration"),
                                        "eval_count": chunk.get("eval_count")
                                    }
                                }
                        except Exception:
                            continue

                    for kind, chunk_text in splitter.flush():
                        yield {"type": kind, "content": chunk_text}

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    name = "Ollama (Local)"
    description = "Generate text using local Ollama models"

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        model = config.get("model", "llama3.2")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "")
        base_url = config.get("base_url", "http://localhost:11434")
        temperature = config.get("temperature", 0.7)
        show_thinking = config.get("thinking", False)
        
        # Structured output: build JSON schema from user-defined custom field defs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)
        
        # Determine if we should use JSON mode
        response_format = config.get("response_format", "text")

        # Heuristic for native reasoning models in Ollama (e.g. DeepSeek R1)
        is_native_reasoner = any(m in model.lower() for m in ["r1", "reasoning", "thought"])
        force_json = (show_thinking and not is_native_reasoner) or (response_format == "json_object")

        effective_prompt = prompt
        if output_schema:
            effective_prompt += format_schema_for_prompt(output_schema)
        elif force_json:
            json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
            effective_prompt += json_hint
        
        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
            )
        
        try:
            # Resolve per-node + workflow-level skills
            all_skills = await resolve_node_skills(config)

            async with httpx.AsyncClient(timeout=300) as client:
                messages = _build_ollama_messages(config, effective_prompt, all_skills)

                req_payload = {
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "options": {
                        "temperature": temperature,
                    },
                    # Use Ollama's native JSON format for structured output
                    **({
                        "format": "json"
                    } if output_schema or response_format == "json_object" else {}),
                }

                # Setup tools if requested either via internal config or node UI toggle
                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                if tools_payload:
                    req_payload["tools"] = tools_payload

                response = await client.post(
                    f"{base_url.rstrip('/')}/api/chat",
                    json=req_payload,
                )
                
                if response.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Ollama error: {response.text}",
                    )
                
                data = response.json()
                msg = data.get("message", {})
                content = msg.get("content", "")
                tool_calls = msg.get("tool_calls")
                
                # Capture thinking if enabled
                captured_thinking = None
                
                # If we forced JSON, parse it
                if force_json:
                    try:
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except (ValueError, AttributeError, TypeError):
                        pass # Fallback

                if show_thinking and not captured_thinking:
                    import re
                    # Many local models (DeepSeek-R1, etc.) use <think> tags
                    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                    if match:
                        captured_thinking = match.group(1).strip()
                    
                    # If model returns reasoning in a separate field (Ollama sometimes does)
                    if not captured_thinking and data.get("message", {}).get("reasoning_content"):
                         captured_thinking = data["message"]["reasoning_content"]

                result_data = {
                    "content": content,
                    "model": model,
                    "total_duration": data.get("total_duration", 0),
                    "eval_count": data.get("eval_count", 0),
                    "input": input_data,
                }
                
                if tool_calls:
                    result_data["tool_calls"] = tool_calls
                
                # Parse structured output and spread fields into result
                if output_schema and content:
                    try:
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("Ollama: Failed to parse structured output as JSON")
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    data=result_data,
                )
                
        except httpx.ConnectError:
            return NodeExecutionResult(
                success=False,
                error=f"Cannot connect to Ollama at {base_url}. Is Ollama running?",
            )
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Ollama request timed out (model may be loading)",
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Ollama error: {str(e)}",
            )

