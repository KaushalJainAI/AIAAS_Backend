"""
LLM Node Handlers

AI/LLM nodes for OpenAI, Google Gemini, and local Ollama integration.
"""
import logging
import httpx
from typing import Any, TYPE_CHECKING

from .base import (
    BaseNodeHandler,
    NodeCategory,
    FieldConfig,
    FieldType,
    HandleDef,
    NodeExecutionResult,
    NodeItem,
    build_json_schema_from_fields,
    format_schema_for_prompt,
)

logger = logging.getLogger(__name__)


def _validate_attachment_path(file_path: str) -> bool:
    """
    Validate that an attachment file path is within the allowed MEDIA_ROOT.
    Prevents path traversal attacks (e.g. ../../../etc/passwd).
    """
    import os
    try:
        from django.conf import settings
        media_root = getattr(settings, 'MEDIA_ROOT', None)
        if not media_root:
            abs_path = os.path.abspath(file_path)
            return '..' not in os.path.relpath(abs_path)
        abs_media = os.path.abspath(media_root)
        abs_file = os.path.abspath(file_path)
        return abs_file.startswith(abs_media)
    except Exception:
        return False

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


async def resolve_node_skills(config: dict[str, Any], context: 'ExecutionContext') -> list[dict]:
    """
    Resolve per-node skill IDs from config, merge with workflow-level context.skills.
    Returns a deduplicated list of skill dicts {title, content}.
    Handles deleted/missing skills gracefully.
    """
    # Start with workflow-level skills
    all_skills = list(context.skills) if context.skills else []
    existing_titles = {s.get('title', '') for s in all_skills}

    # Resolve per-node skill IDs if present
    node_skill_ids = config.get('skills', [])
    if node_skill_ids and isinstance(node_skill_ids, list):
        try:
            from skills.models import Skill
            # Convert to ints for DB query, skip invalid
            valid_ids = []
            for sid in node_skill_ids:
                try:
                    valid_ids.append(int(sid))
                except (ValueError, TypeError):
                    logger.warning(f"Invalid skill ID: {sid}")

            if valid_ids:
                async for skill in Skill.objects.filter(id__in=valid_ids):
                    if skill.title not in existing_titles:
                        all_skills.append({
                            'title': skill.title,
                            'content': skill.content,
                        })
                        existing_titles.add(skill.title)

                # Log any IDs that didn't match (deleted skills)
                found_ids = set()
                async for skill in Skill.objects.filter(id__in=valid_ids).values_list('id', flat=True):
                    found_ids.add(skill)
                missing = set(valid_ids) - found_ids
                if missing:
                    logger.warning(f"Skills not found (may have been deleted): {missing}")
        except Exception as e:
            logger.error(f"Failed to resolve per-node skills: {e}")

    return all_skills


if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


class OpenAINode(BaseNodeHandler):
    """
    Call OpenAI API for text completion/chat.
    
    Supports GPT-4o, GPT-4, GPT-3.5-turbo models.
    """
    
    node_type = "openai"
    name = "OpenAI"
    category = NodeCategory.AI.value
    description = "Generate text using OpenAI GPT models"
    icon = "🤖"
    color = "#10a37f"  # OpenAI green
    static_output_fields = ["content", "model", "usage"]
    
    fields = [
        FieldConfig(
            name="credential",
            label="OpenAI API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="openai",
            description="Select your OpenAI credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="gpt-4o-mini"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt here...",
            description="The prompt to send to the model"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            default="You are a helpful assistant.",
            description="Optional system message to set context"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the system prompt"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.7,
            required=False,
            description="Creativity (0-2, lower = more deterministic)"
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Tokens",
            field_type=FieldType.NUMBER,
            default=1024,
            required=False,
            description="Maximum tokens in response"
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]
    
    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch OpenAI models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="openai", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "gpt-4o-mini" if "gpt-4o-mini" in options else (options[0] if options else "gpt-4o-mini")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for OpenAI: {e}")
            return {}

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        credential_id = config.get("credential")
        model = config.get("model", "gpt-4o-mini")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "OpenAI API key not configured"}
            return

        api_key = api_key.strip()
        is_native_reasoner = any(m in model.lower() for m in ["o1", "o3", "reasoning", "thought", "pro"])
        
        effective_prompt = prompt
        if show_thinking and not is_native_reasoner:
            effective_prompt += "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."

        try:
            all_skills = await resolve_node_skills(config, context)
            async with httpx.AsyncClient(timeout=120) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                
                messages = [{"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"}]
                history = config.get("history", [])
                if history: messages.extend(history)
                
                user_content = [{"type": "text", "text": effective_prompt}]
                attachments = config.get("attachments", [])
                if attachments:
                    import base64
                    for att in attachments:
                        if att.file_type != 'image': continue
                        try:
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            user_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}})
                        except: pass

                messages.append({"role": "user", "content": user_content})

                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True,
                    "stream_options": {"include_usage": True}
                }
                if tools_payload:
                    payload["tools"] = tools_payload

                async with client.stream(
                    "POST", 
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"OpenAI API error: {response.status_code}"}
                        return

                    in_thinking = False
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:].strip()
                        if data_str == "[DONE]": break
                        
                        try:
                            import json
                            chunk = json.loads(data_str)
                            if not chunk.get("choices"):
                                if "usage" in chunk:
                                    yield {"type": "metadata", "usage": chunk["usage"]}
                                continue
                                
                            delta = chunk["choices"][0].get("delta", {})
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                yield {"type": "thinking", "content": delta["reasoning_content"]}
                                continue

                            if "tool_calls" in delta:
                                yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}
                                continue

                            if "content" in delta and delta["content"]:
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}
                        except: continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        model = config.get("model", "gpt-4o-mini")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        # Structured output: build JSON schema from user-defined custom field defs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)
        
        # Determine if we should force JSON for non-native reasoners
        is_native_reasoner = any(m in model.lower() for m in ["o1", "o3", "reasoning", "thought", "pro"])
        force_json = show_thinking and not is_native_reasoner
        
        response_format = config.get("response_format", "text")
        
        effective_prompt = prompt
        if output_schema:
            # Structured output takes priority — append schema to prompt
            effective_prompt += format_schema_for_prompt(output_schema)
        elif response_format == "json_object" or force_json:
            json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format."
            if show_thinking and not is_native_reasoner:
                json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
            effective_prompt += json_hint
        
        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="error"
            )
        
        # Get API key from credentials
        creds = await context.get_credential(credential_id) if credential_id else None
        # Support both 'apiKey' (from schema) and 'api_key' (common)
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            return NodeExecutionResult(
                success=False,
                error="OpenAI API key not configured",
                output_handle="output-0"
            )
            
        # Security: Strip whitespace/newlines which cause 401s
        api_key = api_key.strip()
        
        try:
            # Resolve per-node + workflow-level skills
            all_skills = await resolve_node_skills(config, context)

            async with httpx.AsyncClient(timeout=120) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                }
                
                # Build tools list — when enable_tools is on, include both built-in
                # tools and every MCP tool the user has configured (already cached).
                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                history = config.get("history", [])
                
                messages = [{"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"}]
                
                # Prepend structured history if available
                if history:
                    messages.extend(history)
                    
                attachments = config.get("attachments", [])
                user_msg_content = [{"type": "text", "text": effective_prompt}]
                
                if attachments:
                    import base64
                    for att in attachments:
                        try:
                            # Skip unsupported types for OpenAI (videos, audio, etc.)
                            if att.file_type not in ('image', 'pdf'):
                                logger.info(f"Skipping unsupported attachment type {att.file_type} for OpenAI model {model}")
                                continue
                                
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            # Path traversal protection
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal attempt in attachment: {att.filename}")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            
                            if att.file_type == 'image':
                                user_msg_content.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}
                                })
                            # Note: video type is already filtered out by the file_type check above
                            elif att.file_type == 'pdf':
                                # GPT-4o supports PDF as document blocks in certain regions/API versions
                                # but usually it's better to stick to standard multimodal parts if supported
                                # Anthropic/OpenRouter style 'document' isn't standard OpenAI, 
                                # but OpenAI allows 'input_file' or simply treating it as a file search result.
                                # For consistency with our current Document pipeline, we skip direct PDF block for OpenAI
                                # unless using their File API. For now, we skip to prevent 400.
                                logger.info(f"Skipping PDF attachment for OpenAI model {model} (use RAG instead)")
                                continue

                        except Exception as e:
                            logger.error(f"Failed to attach file {att.filename} to OpenAI request: {e}")

                messages.append({"role": "user", "content": user_msg_content})

                # Check if this is an image or video generation model
                is_img_gen = is_image_generation_model(model)
                is_vid_gen = is_video_generation_model(model)
                
                is_gen = is_img_gen or is_vid_gen
                
                endpoint = "https://api.openai.com/v1/chat/completions"
                if is_img_gen:
                    endpoint = "https://api.openai.com/v1/images/generations"
                elif is_vid_gen:
                    # Sora (OpenAI Video) uses /v1/videos
                    endpoint = "https://api.openai.com/v1/videos"

                payload = {}
                if is_gen:
                    if is_img_gen:
                        payload = {
                            "model": model,
                            "prompt": effective_prompt,
                            "n": 1,
                            "size": config.get("size", "1024x1024"),
                        }
                        if "quality" in config: payload["quality"] = config["quality"]
                        if "style" in config: payload["style"] = config["style"]
                    else:
                        # Sora payload
                        payload = {
                            "model": model,
                            "prompt": effective_prompt,
                        }
                        if "size" in config: payload["size"] = config["size"]
                        if "style" in config: payload["style"] = config["style"]
                else:
                    payload = {
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    }
                    if tools_payload:
                        payload["tools"] = tools_payload

                    # If structured output schema is defined, use OpenAI's native response_format
                    if output_schema:
                        payload["response_format"] = {
                            "type": "json_schema",
                            "json_schema": {
                                "name": "structured_output",
                                "schema": output_schema,
                                "strict": True,
                            }
                        }
                    elif response_format == "json_object":
                        payload["response_format"] = {"type": "json_object"}

                    # Dynamically add custom parameters from the config
                    # Skip keys that belong to customFieldDefs (structured output fields, not API params)
                    output_field_ids = {f.get("id") for f in custom_field_defs} if custom_field_defs else set()
                    for key, value in config.items():
                        if key.startswith("custom_") and value is not None and key not in output_field_ids:
                            param_name = key.replace("custom_", "")
                            payload[param_name] = value

                # ── Agentic tool loop ──────────────────────────────────────────
                # Keeps calling the model until it stops requesting tool calls
                # (or we hit MAX_TOOL_TURNS as a safety cap).
                MAX_TOOL_TURNS = int(config.get("max_tool_turns", 5))
                tool_calls_made: list[dict] = []
                content = ""
                media_url = None
                usage: dict = {}
                finish_reason = "stop"
                data: dict = {}

                import json as _json
                while True:
                    response = await client.post(
                        endpoint,
                        headers=headers,
                        json=payload,
                    )

                    if response.status_code != 200:
                        try:
                            error_data = response.json()
                            error_msg = error_data.get("error", {}).get("message", response.text)
                        except Exception:
                            error_msg = response.text
                        return NodeExecutionResult(
                            success=False,
                            error=f"OpenAI API error: {error_msg}",
                            output_handle="output-0"
                        )

                    data = response.json()

                    if is_gen:
                        # Image / video generation — single call, no tool loop
                        image_data = data.get("data", [{}])[0]
                        media_url = image_data.get("url") or f"data:image/png;base64,{image_data.get('b64_json')}"
                        content = f"Generated image: {media_url}" if not image_data.get("url") else "Image generated successfully."
                        break

                    choice = data["choices"][0]
                    usage = data.get("usage", {})
                    finish_reason = choice.get("finish_reason", "stop")

                    if finish_reason != "tool_calls" or len(tool_calls_made) >= MAX_TOOL_TURNS:
                        content = choice["message"].get("content") or ""
                        break

                    # Execute each requested tool, append results, then loop
                    assistant_msg = choice["message"]
                    messages.append(assistant_msg)

                    from chat.tools import execute_tool as _chat_execute_tool
                    tool_context = {"user_id": context.user_id}

                    for tc in assistant_msg.get("tool_calls", []):
                        tc_id = tc.get("id", "")
                        fn = tc.get("function", {})
                        fn_name = fn.get("name", "")
                        try:
                            fn_args = _json.loads(fn.get("arguments", "{}"))
                        except Exception:
                            fn_args = {}

                        logger.info("OpenAI node tool call: %s args=%s", fn_name, fn_args)
                        result_str = await _chat_execute_tool(fn_name, fn_args, tool_context)
                        tool_calls_made.append({"tool": fn_name, "args": fn_args, "result": result_str})
                        messages.append({
                            "role": "tool",
                            "tool_call_id": tc_id,
                            "content": result_str,
                        })

                    payload["messages"] = messages
                # ── End agentic tool loop ──────────────────────────────────────

                result_data = {
                    "content": content,
                    "model": model,
                    "media_url": media_url,
                    "usage": usage,
                    "finish_reason": finish_reason,
                    "generation_id": data.get("id"),
                }
                if tool_calls_made:
                    result_data["tool_calls"] = tool_calls_made
                
                captured_thinking = None
                
                # If we forced JSON, parse it
                if force_json:
                    try:
                        import json
                        # Basic parsing, might need more robust _parse_json_response later
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except:
                        pass # Fallback to raw content

                if show_thinking and not captured_thinking and not is_gen:
                    # Support OpenAI's specific reasoning field (use safe access to avoid NameError)
                    safe_choice = data.get("choices", [{}])[0]
                    captured_thinking = safe_choice.get("message", {}).get("reasoning_content")
                    
                    # Fallback to <think> tags for models that use them (e.g. fine-tuned)
                    if not captured_thinking:
                        import re
                        match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                        if match:
                            captured_thinking = match.group(1).strip()
                
                result_data.update({
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    "input": input_data,
                })
                
                # Parse structured output and spread fields into result
                if output_schema:
                    try:
                        import json
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("Failed to parse structured output as JSON, returning raw content")
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="OpenAI API request timed out",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"OpenAI error: {str(e)}",
                output_handle="output-0"
            )


class GeminiNode(BaseNodeHandler):
    """
    Call Google Gemini API for text generation.
    
    Supports Gemini 2.0, 1.5 Flash, and Pro models.
    """
    
    node_type = "gemini"
    
    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch Gemini models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="gemini", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "gemini-2.0-flash" if "gemini-2.0-flash" in options else (options[0] if options else "gemini-2.0-flash")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for Gemini: {e}")
            return {}
    name = "Gemini"
    category = NodeCategory.AI.value
    description = "Generate text using Google Gemini models"
    icon = "✨"
    color = "#4285f4"  # Google blue
    static_output_fields = ["content", "model", "usage"]
    
    fields = [
        FieldConfig(
            name="credential",
            label="Gemini API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="gemini-api",
            description="Select your Google AI credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="gemini-2.0-flash"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            description="Sets the behavior and persona of the AI"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the system prompt"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt here...",
            description="The prompt to send to the model"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.7,
            required=False,
            description="Creativity (0-2)"
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Output Tokens",
            field_type=FieldType.NUMBER,
            default=1024,
            required=False,
            description="Maximum tokens in response"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning if supported by the model."
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]
    
    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        model_name = config.get("model", "gemini-1.5-flash")
        user_prompt = config.get("prompt", "")
        sys_message = config.get("system_message", "You are a helpful assistant.")
        temp = config.get("temperature", 0.7)
        tokens_limit = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        credential_id = config.get("credential")
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "Gemini API key not configured"}
            return

        api_key = api_key.strip()
        api_version = "v1"
        if "exp" in model_name or "preview" in model_name:
            api_version = "v1beta"

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                all_skills = await resolve_node_skills(config, context)
                skills_context = format_skills_as_context(all_skills)

                generation_config = {
                    "temperature": temp,
                    "maxOutputTokens": tokens_limit,
                }

                effective_prompt = user_prompt
                if show_thinking:
                    hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
                    effective_prompt += hint

                sys_part = ""
                if sys_message or skills_context:
                    sys_part = f"[SYSTEM INSTRUCTION]\n{sys_message}\n{skills_context}\n\n"

                history = config.get("history", [])
                contents = []
                
                if history:
                    for i, msg in enumerate(history):
                        gemini_role = "model" if msg["role"] == "assistant" else "user"
                        msg_data = [{"text": msg["content"]}]
                        if i == 0 and sys_part and gemini_role == "user":
                            msg_data[0]["text"] = f"{sys_part}{msg_data[0]['text']}"
                            sys_part = ""
                        contents.append({"role": gemini_role, "parts": msg_data})

                final_user_parts = [{"text": effective_prompt}]
                if sys_part:
                    final_user_parts[0]["text"] = f"{sys_part}{final_user_parts[0]['text']}"
                
                # Attachment Handling
                attachments = config.get("attachments", [])
                if attachments:
                    import base64
                    for att in attachments:
                        if att.file_type not in ('image', 'pdf', 'video'): continue
                        try:
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            mime_type = "application/pdf"
                            if att.file_type == 'image': mime_type = "image/jpeg"
                            elif att.file_type == 'video': mime_type = "video/mp4"
                            final_user_parts.append({"inline_data": {"mime_type": mime_type, "data": b64_data}})
                        except: pass

                contents.append({"role": "user", "parts": final_user_parts})

                payload = {
                    "contents": contents,
                    "generationConfig": generation_config,
                }

                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                if tools_payload:
                    gemini_tools = []
                    for t in tools_payload:
                        if t.get("type") == "function":
                            gemini_tools.append({
                                "name": t["function"]["name"],
                                "description": t["function"].get("description", ""),
                                "parameters": t["function"].get("parameters", {})
                            })
                    if gemini_tools:
                        payload["tools"] = [{"functionDeclarations": gemini_tools}]

                # Streaming URL
                url = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_name}:streamGenerateContent"
                
                async with client.stream(
                    "POST", 
                    url, 
                    params={"key": api_key, "alt": "sse"},
                    headers={"Content-Type": "application/json"},
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"Gemini API error: {response.status_code}"}
                        return

                    full_content = ""
                    current_thinking = ""
                    in_thinking = False
                    
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        
                        try:
                            import json
                            chunk = json.loads(line[6:])
                            parts = chunk.get("candidates", [{}])[0].get("content", {}).get("parts", [])
                            for p in parts:
                                if "text" in p:
                                    text = p["text"]
                                    full_content += text
                                    
                                    if "<think>" in text:
                                        in_thinking = True
                                        parts_think = text.split("<think>", 1)
                                        if parts_think[0]: yield {"type": "content", "content": parts_think[0]}
                                        text = parts_think[1]
                                    
                                    if in_thinking:
                                        if "</think>" in text:
                                            parts_think = text.split("</think>", 1)
                                            yield {"type": "thinking", "content": parts_think[0]}
                                            in_thinking = False
                                            if parts_think[1]: yield {"type": "content", "content": parts_think[1]}
                                        else:
                                            yield {"type": "thinking", "content": text}
                                    else:
                                        yield {"type": "content", "content": text}
                                elif "functionCall" in p:
                                    import json
                                    fc = p["functionCall"]
                                    yield {"type": "tool_calls", "tool_calls": [{
                                        "index": 0,
                                        "type": "function",
                                        "function": {
                                            "name": fc["name"],
                                            "arguments": json.dumps(fc.get("args", {}))
                                        }
                                    }]}

                            usage = chunk.get("usageMetadata")
                            if usage:
                                yield {
                                    "type": "metadata",
                                    "usage": {
                                        "prompt_tokens": usage.get("promptTokenCount", 0),
                                        "completion_tokens": usage.get("candidatesTokenCount", 0),
                                        "total_tokens": usage.get("totalTokenCount", 0),
                                    }
                                }
                        except:
                            continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        # 1. Fetch Configuration
        model_name = config.get("model", "gemini-1.5-flash")
        user_prompt = config.get("prompt", "")
        sys_message = config.get("system_message", "You are a helpful assistant.")
        temp = config.get("temperature", 0.7)
        tokens_limit = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        # Determine if we should use JSON mode
        response_format = config.get("response_format", "text")
        
        # Safe extraction for structured outputs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)
        
        if not user_prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="error"
            )
        
        # Get API key from credentials
        credential_id = config.get("credential")
        creds = await context.get_credential(credential_id) if credential_id else None
        # Support both 'apiKey' (from schema) and 'api_key' (common)
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            return NodeExecutionResult(
                success=False,
                error="Gemini API key not configured",
                output_handle="output-0"
            )
            
        # Security: Strip whitespace/newlines
        api_key = api_key.strip()
        
        # Determine API version: 
        # Use v1 for stable (1.5, 2.0, 2.5) unless specifically preview/exp
        api_version = "v1"
        if "exp" in model_name or "preview" in model_name:
            api_version = "v1beta"
        
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                # Resolve per-node + workflow-level skills
                all_skills = await resolve_node_skills(config, context)
                skills_context = format_skills_as_context(all_skills)

                generation_config = {
                    "temperature": temp,
                    "maxOutputTokens": tokens_limit,
                }

                # If structured output schema is defined, use Gemini's native JSON mode
                if output_schema:
                    generation_config["responseMimeType"] = "application/json"
                    generation_config["responseSchema"] = output_schema
                elif response_format == "json_object":
                    generation_config["responseMimeType"] = "application/json"

                # Add custom parameters from config (skip structured output field defs)
                output_field_ids = {f.get("id") for f in custom_field_defs} if custom_field_defs else set()
                for key, value in config.items():
                    if key.startswith("custom_") and value is not None and key not in output_field_ids:
                        param_name = key.replace("custom_", "")
                        if param_name == "top_p":
                            generation_config["topP"] = value
                        elif param_name == "top_k":
                            generation_config["topK"] = value
                        else:
                            generation_config[param_name] = value

                # Build compatible payload by merging system message into prompt
                # (Avoids 'Unknown field system_instruction' on certain API versions/models)
                effective_prompt = user_prompt
                if output_schema:
                    # Append schema hint to prompt for extra reliability
                    effective_prompt += format_schema_for_prompt(output_schema)
                elif show_thinking:
                    # Gemini currently requires force-prompting JSON for reasoning
                    hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
                    effective_prompt += hint

                full_prompt = f"{effective_prompt}"
                sys_part = ""
                if sys_message or skills_context:
                    sys_part = f"[SYSTEM INSTRUCTION]\n{sys_message}\n{skills_context}\n\n"

                # Build native Gemini message history array
                history = config.get("history", [])
                contents = []
                
                # Attachment Handling for Gemini (Multi-modal)
                attachments = config.get("attachments", [])
                extra_parts = []
                if attachments:
                    import base64
                    for att in attachments:
                        try:
                            # Skip unsupported types for Gemini
                            if att.file_type not in ('image', 'pdf', 'video'):
                                logger.info(f"Skipping unsupported attachment type {att.file_type} for Gemini model {model_name}")
                                continue
                                
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            
                            mime_type = "application/pdf"
                            if att.file_type == 'image': mime_type = "image/jpeg"
                            elif att.file_type == 'video': mime_type = "video/mp4"

                            extra_parts.append({
                                "inline_data": {
                                    "mime_type": mime_type,
                                    "data": b64_data
                                }
                            })
                        except Exception as e:
                            logger.error(f"Failed to attach file {att.filename} to Gemini request: {e}")

                # We can't easily prepend a system message as a distinct role in older v1 Gemini APIs,
                # so we prepend the system instruction text to the VERY FIRST message in the history,
                # or the user prompt if there is no history.
                
                if history:
                    for i, msg in enumerate(history):
                        gemini_role = "model" if msg["role"] == "assistant" else "user"
                        msg_data = [{"text": msg["content"]}]
                        
                        # Inject system prompt into the first ever message
                        if i == 0 and sys_part and gemini_role == "user":
                            msg_data[0]["text"] = f"{sys_part}{msg_data[0]['text']}"
                            sys_part = "" # Consumed
                            
                        contents.append({"role": gemini_role, "parts": msg_data})

                # Final user message
                final_user_parts = [{"text": full_prompt}]
                if sys_part:
                    final_user_parts[0]["text"] = f"{sys_part}{final_user_parts[0]['text']}"
                
                # Add current attachments to final user message
                if extra_parts:
                    final_user_parts.extend(extra_parts)

                contents.append({"role": "user", "parts": final_user_parts})

                # Check if this is a video generation model (Veo)
                is_vid_gen = is_video_generation_model(model_name)
                
                endpoint = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_name}:generateContent"
                if is_vid_gen:
                    # Veo uses predictLongRunning for video generation
                    endpoint = f"https://generativelanguage.googleapis.com/{api_version}/models/{model_name}:predictLongRunning"

                payload = {
                    "contents": contents,
                    "generationConfig": generation_config,
                }
                
                if is_vid_gen:
                    # Redefine payload for Veo
                    payload = {
                        "instances": [{"prompt": full_prompt}],
                        "parameters": {
                            "sampleCount": 1,
                        }
                    }
                    if "size" in config: payload["parameters"]["aspectRatio"] = config["size"]

                # Setup tools if requested (map OpenAI schema to Gemini schema)
                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                if tools_payload:
                    gemini_tools = []
                    for t in tools_payload:
                        if t.get("type") == "function":
                            gemini_tools.append({
                                "name": t["function"]["name"],
                                "description": t["function"].get("description", ""),
                                "parameters": t["function"].get("parameters", {})
                            })
                    if gemini_tools:
                        payload["tools"] = [{"functionDeclarations": gemini_tools}]

                response = await client.post(
                    f"https://generativelanguage.googleapis.com/{api_version}/models/{model_name}:generateContent",
                    params={"key": api_key},
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                    },
                    json=payload,
                )
                
                if response.status_code != 200:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", response.text)
                    return NodeExecutionResult(
                        success=False,
                        error=f"Gemini API error: {error_msg}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                candidate     = data["candidates"][0]
                content_parts = candidate.get("content", {}).get("parts", [])
                
                content   = ""
                media_url = None
                
                for part in content_parts:
                    if "text" in part:
                        content += part["text"]
                    elif "inline_data" in part:
                        mime = part["inline_data"].get("mime_type", "image/png")
                        b64  = part["inline_data"].get("data", "")
                        media_url = f"data:{mime};base64,{b64}"
                
                if not content and media_url:
                    content = "Image generated successfully."
                
                usage = data.get("usageMetadata", {})
                
                # Gemini reasoning support (experimental/future-proofing)
                captured_thinking = None
                if show_thinking:
                    # Try to parse forced JSON first
                    try:
                        import json
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except:
                        # Fallback to <think> tags for models that use them
                        import re
                        match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                        if match:
                            captured_thinking = match.group(1).strip()
                
                result_data = {
                    "content": content,
                    "model": model_name,
                    "media_url": media_url,
                    "usage": {
                        "prompt_tokens": usage.get("promptTokenCount", 0),
                        "completion_tokens": usage.get("candidatesTokenCount", 0),
                        "total_tokens": usage.get("totalTokenCount", 0),
                    },
                    "input": input_data,
                }
                
                # Parse structured output and spread fields into result
                if output_schema:
                    try:
                        import json
                        parsed = json.loads(content) if isinstance(content, str) else content
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("Gemini: Failed to parse structured output as JSON")
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Gemini API request timed out",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Gemini error: {str(e)}",
                output_handle="output-0"
            )


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
            all_skills = await resolve_node_skills(config, context)
            async with httpx.AsyncClient(timeout=300) as client:
                messages = []
                if system_message or all_skills:
                    messages.append({"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"})
                
                history = config.get("history", [])
                if history: messages.extend(history)
                
                user_content = [{"type": "text", "text": effective_prompt}]
                attachments = config.get("attachments", [])
                if attachments:
                    import base64
                    for att in attachments:
                        if att.file_type != 'image': continue
                        try:
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            user_content.append({"type": "image", "image": b64_data})
                        except: pass
                
                messages.append({"role": "user", "content": user_content if len(user_content) > 1 else effective_prompt})

                req_payload = {
                    "model": model,
                    "messages": messages,
                    "stream": True,
                    "options": {"temperature": temperature},
                }

                in_thinking = False
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
                            import json
                            chunk = json.loads(line)
                            msg = chunk.get("message", {})
                            text = msg.get("content", "")
                            
                            if text:
                                if "reasoning_content" in msg and msg["reasoning_content"]:
                                     yield {"type": "thinking", "content": msg["reasoning_content"]}

                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}
                            
                            if chunk.get("done"):
                                yield {
                                    "type": "metadata",
                                    "usage": {
                                        # Map Ollama metrics to standard keys
                                        "total_duration": chunk.get("total_duration"),
                                        "eval_count": chunk.get("eval_count")
                                    }
                                }
                        except: continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch Ollama models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="ollama", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "llama3.2:latest" if "llama3.2:latest" in options else (options[0] if options else "llama3.2:latest")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for Ollama: {e}")
            return {}
    name = "Ollama (Local)"
    category = NodeCategory.AI.value
    description = "Generate text using local Ollama models"
    icon = "🦙"
    color = "#0d1117"  # Ollama dark
    static_output_fields = ["content", "model"]
    
    fields = [
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="llama3.2:latest"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt here...",
            description="The prompt to send to the model"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            default="",
            description="Optional system message"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the system prompt"
        ),
        FieldConfig(
            name="base_url",
            label="Ollama URL",
            field_type=FieldType.STRING,
            default="http://localhost:11434",
            required=False,
            description="Ollama server URL"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.7,
            required=False,
            description="Creativity (0-2)"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning (e.g. <think> tags) and shows it to the user."
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]
    
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
                output_handle="error"
            )
        
        try:
            # Resolve per-node + workflow-level skills
            all_skills = await resolve_node_skills(config, context)

            async with httpx.AsyncClient(timeout=300) as client:
                # Build messages
                history = config.get("history", [])
                messages = []
                
                if system_message:
                    messages.append({"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"})
                elif all_skills:
                    messages.append({"role": "system", "content": format_skills_as_context(all_skills)})
                
                if history:
                    messages.extend(history)
                    
                attachments = config.get("attachments", [])
                user_msg_parts = [{"type": "text", "text": effective_prompt}]
                
                if attachments:
                    import base64
                    for att in attachments:
                        try:
                            # Ollama (multimodal models) typically supports only images
                            if att.file_type != 'image':
                                logger.info(f"Skipping unsupported attachment type {att.file_type} for Ollama model {model}")
                                continue
                                
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            
                            user_msg_parts.append({
                                "type": "image",
                                "image": b64_data
                            })
                        except Exception as e:
                            logger.error(f"Failed to attach file {att.filename} to Ollama request: {e}")

                messages.append({"role": "user", "content": user_msg_parts if len(user_msg_parts) > 1 else effective_prompt})
                
                # Build requests payload
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
                        output_handle="output-0"
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
                        import json
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except:
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
                        import json
                        parsed = json.loads(content)
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("Ollama: Failed to parse structured output as JSON")
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except httpx.ConnectError:
            return NodeExecutionResult(
                success=False,
                error=f"Cannot connect to Ollama at {base_url}. Is Ollama running?",
                output_handle="output-0"
            )
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Ollama request timed out (model may be loading)",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Ollama error: {str(e)}",
                output_handle="output-0"
            )

class PerplexityNode(BaseNodeHandler):
    """
    Call Perplexity API for web-grounded AI responses.
    
    Supports Sonar models with real-time web search capabilities.
    """
    
    node_type = "perplexity"

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        credential_id = config.get("credential")
        model = config.get("model", "sonar")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "Be precise and concise.")
        temperature = config.get("temperature", 0.2)
        max_tokens = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "Perplexity API key not configured"}
            return

        api_key = api_key.strip()
        
        try:
            all_skills = await resolve_node_skills(config, context)
            async with httpx.AsyncClient(timeout=120) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json"
                }
                
                messages = [{"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"}]
                history = config.get("history", [])
                if history: messages.extend(history)
                messages.append({"role": "user", "content": prompt})

                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True
                }
                if tools_payload:
                    payload["tools"] = tools_payload

                in_thinking = False
                async with client.stream(
                    "POST", 
                    "https://api.perplexity.ai/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"Perplexity API error: {response.status_code}"}
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:].strip()
                        if not data_str: continue
                        
                        try:
                            import json
                            chunk = json.loads(data_str)
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                yield {"type": "thinking", "content": delta["reasoning_content"]}
                                continue

                            if "tool_calls" in delta:
                                yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}
                                continue

                            if "content" in delta and delta["content"]:
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}

                            if chunk.get("usage"):
                                yield {"type": "metadata", "usage": chunk["usage"]}
                            
                            if chunk.get("citations"):
                                yield {"type": "citations", "citations": chunk["citations"]}

                        except: continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}

    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch Perplexity models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="perplexity", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "sonar" if "sonar" in options else (options[0] if options else "sonar")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for Perplexity: {e}")
            return {}
    name = "Perplexity"
    category = NodeCategory.AI.value
    description = "Generate web-grounded answers using Perplexity AI"
    icon = "🔍"
    color = "#1FB1E6"  # Perplexity blue
    static_output_fields = ["content", "model", "usage", "citations"]
    fields = [
        FieldConfig(
            name="credential",
            label="Perplexity API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="perplexity-api",
            description="Select your Perplexity API credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="sonar"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your question or prompt...",
            description="The prompt to send to Perplexity"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            default="Be precise and concise.",
            description="Optional system message to set behavior"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the system prompt"
        ),
        FieldConfig(
            name="search_domain_filter",
            label="Domain Filter",
            field_type=FieldType.STRING,
            required=False,
            placeholder="e.g., github.com, stackoverflow.com",
            description="Comma-separated domains to search (optional)"
        ),
        FieldConfig(
            name="search_recency_filter",
            label="Recency Filter",
            field_type=FieldType.SELECT,
            options=["none", "day", "week", "month", "year"],
            default="none",
            required=False,
            description="Filter sources by recency"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.2,
            required=False,
            description="Creativity (0-2, lower = more factual)"
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Tokens",
            field_type=FieldType.NUMBER,
            default=1024,
            required=False,
            description="Maximum tokens in response"
        ),
        FieldConfig(
            name="return_citations",
            label="Return Citations",
            field_type=FieldType.BOOLEAN,
            default=True,
            required=False,
            description="Include source URLs in response"
        ),
        FieldConfig(
            name="return_images",
            label="Return Images",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Include related images if available"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning if supported."
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]
    
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        credential_id = config.get("credential")
        model = config.get("model", "sonar")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "Be precise and concise.")
        temperature = config.get("temperature", 0.2)
        max_tokens = config.get("max_tokens", 1024)
        domain_filter = config.get("search_domain_filter", "")
        recency_filter = config.get("search_recency_filter", "none")
        return_citations = config.get("return_citations", True)
        return_images = config.get("return_images", False)
        show_thinking = config.get("thinking", False)

        # Structured output: build JSON schema from user-defined custom field defs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)

        # Heuristic for native reasoning models (e.g., some fine-tunes)
        is_native_reasoner = any(m in model.lower() for m in ["r1", "reasoning", "thought"])
        force_json = show_thinking and not is_native_reasoner
        
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
                output_handle="error"
            )
        
        # Get API key from credentials
        creds = await context.get_credential(credential_id) if credential_id else None
        # Support both 'apiKey' (from schema) and 'api_key' (common)
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            return NodeExecutionResult(
                success=False,
                error="Perplexity API key not configured",
                output_handle="output-0"
            )
            
        # Security: Strip whitespace/newlines which cause 401 HTML responses from gateways
        api_key = api_key.strip()
        
        try:
            # Build request payload
            # Resolve per-node + workflow-level skills
            all_skills = await resolve_node_skills(config, context)

            messages = [
                {"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"},
            ]
            
            history = config.get("history", [])
            if history:
                messages.extend(history)
            
            # Perplexity does not support direct media attachments in the chat completions API
            attachments = config.get("attachments", [])
            if attachments:
                for att in attachments:
                    logger.info(f"Skipping attachment {att.filename} for Perplexity (unsupported type: {att.file_type})")

            messages.append({"role": "user", "content": effective_prompt})
            
            payload = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "return_citations": return_citations,
                "return_images": return_images,
            }
            
            # Add optional search filters
            if domain_filter:
                domains = [d.strip() for d in domain_filter.split(",") if d.strip()]
                if domains:
                    payload["search_domain_filter"] = domains
            
            if recency_filter and recency_filter != "none":
                payload["search_recency_filter"] = recency_filter
            
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://api.perplexity.ai/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36"
                    },
                    json=payload,
                )
                
                if response.status_code != 200:
                    error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    error_msg = error_data.get("error", {}).get("message", response.text) if isinstance(error_data.get("error"), dict) else str(error_data.get("error", response.text))
                    return NodeExecutionResult(
                        success=False,
                        error=f"Perplexity API error: {error_msg}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                citations = data.get("citations", [])
                images = data.get("images", [])
                
                result_data = {
                    "content": content,
                    "model": model,
                    "usage": {
                        "prompt_tokens": usage.get("prompt_tokens", 0),
                        "completion_tokens": usage.get("completion_tokens", 0),
                        "total_tokens": usage.get("total_tokens", 0),
                    },
                    "input": input_data,
                }
                
                # Add optional fields if present
                if return_citations and citations:
                    result_data["citations"] = citations
                
                if return_images and images:
                    result_data["images"] = images
                
                # Perplexity reasoning support (future-proofing)
                captured_thinking = None
                if force_json:
                    try:
                        import json
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except:
                        pass # Fallback to raw content

                if show_thinking and not captured_thinking:
                    import re
                    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                    if match:
                        captured_thinking = match.group(1).strip()
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                # Parse structured output and spread fields into result
                if output_schema:
                    try:
                        import json
                        raw = result_data.get("content", "")
                        parsed = json.loads(raw.strip().strip("```json").strip("```"))
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("Perplexity: Failed to parse structured output as JSON")

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Perplexity API request timed out",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Perplexity error: {str(e)}",
                output_handle="output-0"
            )


class OpenRouterNode(BaseNodeHandler):
    """
    Call OpenRouter API for unified access to many LLM models.
    OpenRouter provides access to GPT-4, Claude, Llama, Mistral, Gemini, and many more
    through a single API with unified pricing.
    """

    node_type = "openrouter"

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        credential_id = config.get("credential")
        model = config.get("model", "openai/gpt-4o-mini")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 1024)
        show_thinking = config.get("thinking", False)
        
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "OpenRouter API key not configured"}
            return

        api_key = api_key.strip()
        
        try:
            all_skills = await resolve_node_skills(config, context)
            async with httpx.AsyncClient(timeout=120) as client:
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                    "HTTP-Referer": "https://aiaas.com",
                    "X-Title": "AIAAS",
                }
                
                messages = [{"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"}]
                history = config.get("history", [])
                if history: messages.extend(history)
                messages.append({"role": "user", "content": prompt})

                tools_payload: list | None = list(config.get("tools") or [])
                enable_tools_ui = config.get("enable_tools", False)
                if enable_tools_ui:
                    from chat.tools import get_available_tools as _get_tools
                    tools_payload = await _get_tools(context.user_id)

                payload = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "stream": True
                }
                if tools_payload:
                    payload["tools"] = tools_payload

                in_thinking = False
                async with client.stream(
                    "POST", 
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"OpenRouter API error: {response.status_code}"}
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:].strip()
                        if not data_str or data_str == "[DONE]": continue
                        
                        try:
                            import json
                            chunk = json.loads(data_str)
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            
                            if "reasoning" in delta and delta["reasoning"]:
                                yield {"type": "thinking", "content": delta["reasoning"]}
                                continue
                            
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                yield {"type": "thinking", "content": delta["reasoning_content"]}
                                continue

                            if "tool_calls" in delta:
                                yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}
                                continue

                            if "content" in delta and delta["content"]:
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}

                            if chunk.get("usage"):
                                yield {"type": "metadata", "usage": chunk["usage"]}

                        except: continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}
    name = "OpenRouter"
    category = NodeCategory.AI.value
    description = "Access 100+ AI models through OpenRouter (GPT-4, Claude, Llama, Mistral, etc.)"
    icon = "🌐"
    color = "#6366f1"
    static_output_fields = ["content", "model", "usage"]

    # Safe fallback used when selected model returns 404
    # Pick something that is both strong and clearly free.
    FALLBACK_MODEL = "meta-llama/llama-3.3-70b-instruct:free"

    fields = [
        FieldConfig(
            name="credential",
            label="OpenRouter API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="openrouter",
            description="Select your OpenRouter credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            # Default to robust, high‑quality free model
            default="meta-llama/llama-3.3-70b-instruct:free"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt here...",
            description="The prompt to send to the model"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            default="You are a helpful assistant.",
            description="Optional system message to set context"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the system prompt"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.3,
            required=False,
            description="Creativity (0-2, lower = more deterministic)"
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Tokens",
            field_type=FieldType.NUMBER,
            default=2048,
            required=False,
            description="Maximum number of tokens to generate."
        ),
        FieldConfig(
            name="top_p",
            label="Top P",
            field_type=FieldType.NUMBER,
            default=1.0,
            required=False,
            description="Nucleus sampling (0-1)"
        ),
        FieldConfig(
            name="response_format",
            label="Response Format",
            field_type=FieldType.SELECT,
            options=["text", "json_object", "json_code"],
            default="text",
            required=False,
            description="Output format: plain text, forced JSON, or structured Python Code JSON"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning (o1/o3 reasoning_content)."
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch OpenRouter models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="openrouter", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "meta-llama/llama-3.3-70b-instruct:free" 
                                    if "meta-llama/llama-3.3-70b-instruct:free" in options 
                                    else (options[0] if options else "openrouter/auto")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for OpenRouter: {e}")
            return {}

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: "ExecutionContext"
    ) -> NodeExecutionResult:
        # ── Config ────────────────────────────────────────────────────────────
        credential_id  = config.get("credential")
        model          = config.get("model", self.FALLBACK_MODEL)
        prompt         = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        show_thinking  = config.get("thinking", False)

        # Structured output: build JSON schema from user-defined custom field defs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)

        response_format = config.get("response_format", "text")
        force_json = response_format in ["json_object", "json_code"]
        captured_thinking = None
        
        effective_prompt = prompt
        if output_schema:
            effective_prompt += format_schema_for_prompt(output_schema)
        elif response_format == "json_code":
            json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning), 'explanation' (brief summary), and 'code' (just the Python code string, no markdown fences)."
            effective_prompt += json_hint
        elif response_format == "json_object":
            json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
            effective_prompt += json_hint

        # Defensive numeric parsing
        try:
            temperature = float(config.get("temperature", 0.3))
        except (ValueError, TypeError):
            temperature = 0.3

        try:
            max_tokens = int(config.get("max_tokens", 2048))
        except (ValueError, TypeError):
            max_tokens = 2048

        try:
            top_p = float(config.get("top_p", 1.0))
        except (ValueError, TypeError):
            top_p = 1.0

        # ── Validation ────────────────────────────────────────────────────────
        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="output-0"
            )

        # ── Credentials ───────────────────────────────────────────────────────
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None

        if not api_key:
            return NodeExecutionResult(
                success=False,
                error="OpenRouter API key not configured",
                output_handle="output-0"
            )

        api_key = api_key.strip()

        try:
            import mimetypes
            # ── Skills + Messages ─────────────────────────────────────────────
            all_skills    = await resolve_node_skills(config, context)
            skill_context = format_skills_as_context(all_skills)
            
            # Combine system message and skills
            full_system_msg = f"{system_message}{skill_context}"

            messages = []
            
            history = config.get("history", [])
            if history:
                messages.extend(history)
                
            user_msg_content = [{"type": "text", "text": effective_prompt}]
            
            attachments = config.get("attachments", [])
            if attachments:
                import base64
                for att in attachments:
                    try:
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            
                            # Dynamically determine MIME type
                            mime_type, _ = mimetypes.guess_type(file_path)
                            if not mime_type:
                                if att.file_type == 'image': mime_type = "image/jpeg"
                                elif att.file_type == 'pdf': mime_type = "application/pdf"
                                else: mime_type = "application/octet-stream"

                            if att.file_type == 'image': 
                                # OpenRouter/Anthropic multimodal format
                                user_msg_content.append({
                                    "type": "image_url",
                                    "image_url": {"url": f"data:{mime_type};base64,{b64_data}"}
                                })
                            elif att.file_type == 'pdf':
                                # OpenRouter PDF input format (uses "file" type with metadata)
                                user_msg_content.append({
                                    "type": "file",
                                    "file": {
                                        "filename": att.filename,
                                        "file_data": f"data:{mime_type};base64,{b64_data}"
                                    }
                                })
                            else:
                                # Skip unsupported types like video/audio for standard OpenRouter/Anthropic blocks
                                logger.info(f"Skipping unsupported attachment type {att.file_type} for OpenRouter model {model}")
                                continue
                    except Exception as e:
                        logger.error(f"Failed to attach file {att.filename} to OpenRouter request: {e}")

            messages.append({"role": "user", "content": user_msg_content})

            # ── Request Body ──────────────────────────────────────────────────
            is_gen = is_image_generation_model(model)
            
            # Switch to dedicated images endpoint for pure generators
            # This fixes "400 Bad Request" when using /chat/completions for non-multimodal models
            endpoint = "https://openrouter.ai/api/v1/images/generations" if is_gen else "https://openrouter.ai/api/v1/chat/completions"
            
            # Special case: If user intent is image/video, force specific handling for some providers
            # but OpenRouter unified endpoint prefers /chat/completions with modalities unless it's a pure generator
            
            body: dict[str, Any] = {
                "model":       model,
                "messages":    messages,
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "top_p":       top_p,
            }

            if is_gen:
                # For /images/generations, OpenRouter/OpenAI-compatible payload prefers 'prompt'
                body["prompt"] = prompt
                body["modalities"] = ["image"]
                
                # Strip chat history for pure generators to avoid "Provider returned error" (400)
                # Some providers (like Black Forest Labs via OpenRouter) fail if they see "system" or multiple messages
                body["messages"] = [{"role": "user", "content": prompt}]
                if "system_message" in config:
                    # If we have a system prompt, we can try to prepend it to the user prompt if it's a pure generator
                    body["messages"][0]["content"] = f"{config['system_message']}\n\n{prompt}"
                
                # Remove fields not supported by /images/generations if necessary
                if "temperature" in body: del body["temperature"]
                if "top_p" in body: del body["top_p"]
            else:
                # Standard chat payload
                pass

            # Optional: Move system message to a top-level field for better provider compatibility if needed

            # Add system message to the messages array (OpenRouter/OpenAI standard)
            # Avoiding body["system"] as some providers reject unknown top-level fields.
            if full_system_msg:
                messages.insert(0, {"role": "system", "content": [{"type": "text", "text": full_system_msg}]})
            else:
                messages.insert(0, {"role": "system", "content": [{"type": "text", "text": "You are a helpful assistant."}]})

            # Setup tools if requested either via internal config or node UI toggle
            tools_payload = config.get("tools")
            enable_tools_ui = config.get("enable_tools", False)
            if not tools_payload and enable_tools_ui:
                import chat.tools as shared_tools
                tools_payload = shared_tools.AVAILABLE_TOOLS

            if tools_payload:
                body["tools"] = tools_payload

            # Use structured output schema if defined, otherwise use manual response_format
            if output_schema:
                body["response_format"] = {
                    "type": "json_schema",
                    "json_schema": {
                        "name": "structured_output",
                        "schema": output_schema,
                        "strict": True,
                    }
                }
            elif response_format in ["json_object", "json_code"]:
                body["response_format"] = {"type": "json_object"}

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                # These are recommended but optional headers. [web:33][web:39]
                "HTTP-Referer":  "https://aiaas.local",
                "X-Title":       "AIAAS Workflow",
            }

            # ── API Call (with 404 auto-fallback and 5xx retries) ─────────────────────────────
            MAX_RETRIES = 3
            retry_count = 0
            actual_model = model
            used_fallback = False

            async with httpx.AsyncClient(timeout=180) as client:
                while retry_count < MAX_RETRIES:
                    try:
                        response = await client.post(
                            endpoint,
                            headers=headers,
                            json=body,
                        )

                        # 404: model no longer available — retry once with fallback
                        if response.status_code == 404 and body["model"] != self.FALLBACK_MODEL:
                            body["model"] = self.FALLBACK_MODEL
                            used_fallback = True
                            actual_model = self.FALLBACK_MODEL
                            continue # Try immediately with fallback

                        # 400: Potential 'response_format' rejection for non-compatible models
                        if response.status_code == 400 and "response_format" in body:
                            error_txt = response.text.lower()
                            if any(x in error_txt for x in ["response_format", "json_mode", "json_object"]):
                                logger.warning("OpenRouter: model rejected response_format. Retrying without it.")
                                del body["response_format"]
                                continue

                        # If 5xx, retry
                        if 500 <= response.status_code < 600:
                            retry_count += 1
                            if retry_count < MAX_RETRIES:
                                logger.warning(f"OpenRouter 5xx error ({response.status_code}). Retrying {retry_count}/{MAX_RETRIES}...")
                                await asyncio.sleep(1 * retry_count) # Exponential backoff
                                continue

                        # If we reach here, either 200 or an error we don't retry (4xx other than 404)
                        break

                    except (httpx.ConnectError, httpx.TimeoutException) as e:
                        retry_count += 1
                        if retry_count >= MAX_RETRIES:
                            return NodeExecutionResult(
                                success=False,
                                error=f"OpenRouter connection failed after {MAX_RETRIES} attempts: {str(e)}",
                                output_handle="output-0"
                            )
                        await asyncio.sleep(1 * retry_count)

            # ── Non-200 after all attempts ────────────────────────────────
            if response.status_code != 200:
                error_msg = response.text
                try:
                    error_data = response.json()
                    err = error_data.get("error", {})

                    if isinstance(err, dict):
                        base_msg = err.get("message", "Unknown error")
                        metadata = error_data.get("metadata", {})
                        raw_error = metadata.get("raw", "")

                        if raw_error:
                            error_msg = f"{base_msg} | Provider Detail: {raw_error}"
                        else:
                            error_msg = base_msg
                    else:
                        error_msg = str(err)
                except Exception:
                    pass

                return NodeExecutionResult(
                    success=False,
                    error=f"OpenRouter API error ({response.status_code}): {error_msg}",
                    output_handle="output-0"
                )

            # ── Parse Response ────────────────────────────────────────────────
            data = response.json()
            choices = data.get("choices", [])
            
            # If no choices, check if it's a dedicated image generation response (which has 'data')
            if not choices and "data" not in data:
                return NodeExecutionResult(
                    success=False,
                    error="OpenRouter API returned no choices or image data",
                    output_handle="output-0"
                )

            # Default values
            content       = ""
            finish_reason = "unknown"
            generation_id = data.get("id", "")
            usage         = data.get("usage", {})
            media_url     = None

            if choices:
                choice        = choices[0]
                content       = choice.get("message", {}).get("content", "")
                finish_reason = choice.get("finish_reason", "unknown")
                
                # Check for multimodal image output (Imagen style)
                msg_images = choice.get("message", {}).get("images", [])
                if msg_images and isinstance(msg_images, list):
                    # Usually a list of base64 strings
                    media_url = msg_images[0]
                    if not media_url.startswith("data:"):
                        media_url = f"data:image/png;base64,{media_url}"
                    if not content:
                        content = "Image generated successfully."

            # Check for OpenAI-style image data if routed via completions
            elif "data" in data and isinstance(data["data"], list) and len(data["data"]) > 0:
                img_data = data["data"][0]
                media_url = img_data.get("url") or f"data:image/png;base64,{img_data.get('b64_json')}"
                if not content:
                    content = "Image generated successfully."

            result_data = {
                "content": content,
                "model": actual_model,
                "media_url": media_url,
                "usage": usage,
                "finish_reason": finish_reason,
                "generation_id": generation_id
            }

            # Parse forced JSON if requested
            parsed_json = {}
            if force_json:
                try:
                    import json
                    parsed_json = json.loads(content.strip().strip("```json").strip("```"))
                    if isinstance(parsed_json, dict):
                        captured_thinking = parsed_json.get("thinking")
                        # For content, prioritize 'content' or 'code' (for json_code)
                        content = parsed_json.get("content") or parsed_json.get("code") or content
                except:
                    pass

            if show_thinking and not captured_thinking:
                captured_thinking = choice.get("message", {}).get("reasoning", "")
                
                # Some providers might put it in 'thinking' or other fields
                if not captured_thinking:
                    captured_thinking = choice.get("message", {}).get("thinking", "")
                
                # Fallback to <think> tags
                if not captured_thinking:
                    import re
                    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                    if match:
                        captured_thinking = match.group(1).strip()

            result_data = {
                "content":         content,
                "model":           data.get("model", actual_model),
                "requested_model": model,
                "used_fallback":   used_fallback,
                "generation_id":   generation_id,
                "finish_reason":   finish_reason,
                "usage": {
                    "prompt_tokens":     usage.get("prompt_tokens", 0),
                    "completion_tokens": usage.get("completion_tokens", 0),
                    "total_tokens":      usage.get("total_tokens", 0),
                },
                "input": input_data,
            }

            # Capture tool calls for OpenAI compatibility mapping
            tool_calls = choice.get("message", {}).get("tool_calls")
            if tool_calls:
                result_data["tool_calls"] = tool_calls
            
            # Merge extra fields from parsed JSON (except thinking which goes to its own field)
            if parsed_json and isinstance(parsed_json, dict):
                for k, v in parsed_json.items():
                    if k != "thinking":
                        result_data[k] = v
            
            if captured_thinking:
                result_data["thinking"] = captured_thinking

            # Parse structured output and spread fields into result
            if output_schema and content:
                try:
                    import json
                    raw = result_data.get("content", "")
                    parsed = json.loads(raw) if isinstance(raw, str) else raw
                    if isinstance(parsed, dict):
                        result_data.update(parsed)
                except Exception:
                    logger.warning("OpenRouter: Failed to parse structured output as JSON")

            return NodeExecutionResult(
                success=True,
                items=[NodeItem(json=result_data)],
                output_handle="output-0"
            )

        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="OpenRouter API request timed out (180s)",
                output_handle="output-0"
            )
        except KeyError as e:
            return NodeExecutionResult(
                success=False,
                error=f"Unexpected OpenRouter response shape — missing key: {e}",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"OpenRouter error: {str(e)}",
                output_handle="output-0"
            )


class HuggingFaceNode(BaseNodeHandler):
    """
    Call Hugging Face Inference API for text generation.
    """
    
    node_type = "huggingface"

    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch Hugging Face models from database"""
        try:
            from nodes.models import AIModel
            # Note: HuggingFaceNode usually uses model IDs, but we can still fetch registered models
            models = AIModel.objects.filter(provider__slug="huggingface", is_active=True).values_list('value', flat=True)
            options = list(models)
            if not options:
                 # Default if nothing in DB
                 return {}
            return {
                "model": {
                    "options": options,
                    "defaultValue": "meta-llama/Meta-Llama-3-8B-Instruct" if "meta-llama/Meta-Llama-3-8B-Instruct" in options else options[0]
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for HuggingFace: {e}")
            return {}
    name = "Hugging Face"
    category = NodeCategory.AI.value
    description = "Generate text using Hugging Face models"
    icon = "🤗"
    color = "#FFD21E"  # Hugging Face yellow
    static_output_fields = ["content", "model"]
    
    fields = [
        FieldConfig(
            name="credential",
            label="Hugging Face API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="huggingface-api",
            description="Select your Hugging Face credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="meta-llama/Meta-Llama-3-8B-Instruct",
            description="Select a Hugging Face model from your registered list"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt here...",
            description="The prompt to send to the model"
        ),
        FieldConfig(
            name="system_message",
            label="System Message",
            field_type=FieldType.STRING,
            required=False,
            default="",
            description="Optional system message to set context (if supported by the model)"
        ),
        FieldConfig(
            name="skills",
            label="Skills",
            field_type=FieldType.SKILLS,
            required=False,
            description="Select skills to inject as context into the prompt"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.7,
            required=False,
            description="Creativity (0-2)"
        ),
        FieldConfig(
            name="enable_tools",
            label="Enable Tools (Agentic)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="Allow the model to dynamically call external tools (e.g., search, workflow execution)."
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Tokens",
            field_type=FieldType.NUMBER,
            default=512,
            required=False,
            description="Maximum tokens to generate"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        model = config.get("model", "meta-llama/Meta-Llama-3-8B-Instruct")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "")
        temperature = config.get("temperature", 0.7)
        max_new_tokens = int(config.get("max_tokens", 512))
        credential_id = config.get("credential")
        
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "Hugging Face API key not configured"}
            return
            
        api_key = api_key.strip()
        
        try:
            all_skills = await resolve_node_skills(config, context)
            skills_context = format_skills_as_context(all_skills)
            
            history = config.get("history", [])
            history_str = ""
            if history:
                history_parts = []
                for msg in history:
                    role_str = "Assistant" if msg["role"] == "assistant" else "User"
                    history_parts.append(f"{role_str}: {msg['content']}")
                history_str = "\n".join(history_parts) + "\n"
            
            full_prompt = prompt
            if system_message or skills_context:
                full_prompt = f"System: {system_message}\n{skills_context}\n{history_str}User: {prompt}"
            elif history_str:
                full_prompt = f"{history_str}User: {prompt}"

            payload = {
                "inputs": full_prompt,
                "parameters": {
                    "temperature": temperature,
                    "max_new_tokens": max_new_tokens,
                    "return_full_text": False
                },
                "stream": True
            }

            async with httpx.AsyncClient(timeout=120) as client:
                in_thinking = False
                async with client.stream(
                    "POST", 
                    f"https://api-inference.huggingface.co/models/{model}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"Hugging Face API error: {response.status_code}"}
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data:"): continue
                        try:
                            import json
                            chunk = json.loads(line[5:].strip())
                            text = chunk.get("token", {}).get("text", "")
                            
                            if text:
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}
                                    
                        except: continue

        except Exception as e:
            yield {"type": "error", "message": f"Hugging Face error: {str(e)}"}

    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        model = config.get("model", "meta-llama/Meta-Llama-3-8B-Instruct")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "")
        temperature = config.get("temperature", 0.7)
        max_new_tokens = int(config.get("max_tokens", 512)) # Renamed from max_new_tokens to max_tokens
        show_thinking = config.get("thinking", False)
        credential_id = config.get("credential")
        
        # Structured output: build JSON schema from user-defined custom field defs
        custom_field_defs = config.get("customFieldDefs", [])
        output_schema = build_json_schema_from_fields(custom_field_defs)
        
        # HuggingFace prompt engineering for reasoning
        # (Most HF Inference models work better with clear JSON hints)
        force_json = show_thinking
        effective_prompt = prompt
        if output_schema:
            effective_prompt += format_schema_for_prompt(output_schema)
        elif force_json:
            json_hint = "\n\nIMPORTANT: Respond ONLY in JSON format with fields 'thinking' (your reasoning) and 'content' (your actual answer)."
            effective_prompt = f"{effective_prompt}{json_hint}"

        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="error"
            )
            
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = creds.get("apiKey") or creds.get("api_key") if creds else None
        
        if not api_key:
            return NodeExecutionResult(
                success=False,
                error="Hugging Face API key not configured",
                output_handle="output-0"
            )
            
        api_key = api_key.strip()
        
        try:
            all_skills = await resolve_node_skills(config, context)
            skills_context = format_skills_as_context(all_skills)
            
            # Combine system message and skills with prompt (basic formatting)
            full_prompt = prompt
            
            # Hugging Face Inference API does not support direct media attachments in the standard text generation endpoint
            attachments = config.get("attachments", [])
            if attachments:
                for att in attachments:
                    logger.info(f"Skipping attachment {att.filename} for Hugging Face (unsupported type: {att.file_type})")

            history = config.get("history", [])
            history_str = ""
            if history:
                history_parts = []
                for msg in history:
                    role_str = "Assistant" if msg["role"] == "assistant" else "User"
                    history_parts.append(f"{role_str}: {msg['content']}")
                history_str = "\n".join(history_parts) + "\n"
            
            if system_message or skills_context:
                full_prompt = f"System: {system_message}\n{skills_context}\n{history_str}User: {prompt}"
            elif history_str:
                full_prompt = f"{history_str}User: {prompt}"
                
            payload = {
                "inputs": full_prompt,
                "parameters": {
                    "temperature": temperature,
                    "max_new_tokens": max_new_tokens,
                    "return_full_text": False
                }
            }
            
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"https://api-inference.huggingface.co/models/{model}",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                )
                
                if response.status_code != 200:
                    error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    error_msg = error_data.get("error", response.text)
                    return NodeExecutionResult(
                        success=False,
                        error=f"Hugging Face API error: {error_msg}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                content = ""
                if isinstance(data, list) and len(data) > 0:
                    content = data[0].get("generated_text", "")
                elif isinstance(data, dict):
                    content = data.get("generated_text", "")

                # HF reasoning extraction
                captured_thinking = None
                
                # If we forced JSON, parse it
                if force_json:
                    try:
                        import json
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        captured_thinking = parsed.get("thinking")
                        content = parsed.get("content", content)
                    except:
                        pass # Fallback

                if show_thinking and not captured_thinking:
                    import re
                    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                    if match:
                        captured_thinking = match.group(1).strip()

                result_data = {
                    "content": content,
                    "model": model,
                    "input": input_data,
                }
                
                # Parse structured output and spread fields into result
                if output_schema:
                    try:
                        import json
                        parsed = json.loads(content.strip().strip("```json").strip("```"))
                        if isinstance(parsed, dict):
                            result_data.update(parsed)
                    except Exception:
                        logger.warning("HuggingFace: Failed to parse structured output as JSON")
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Hugging Face API request timed out",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Hugging Face error: {str(e)}",
                output_handle="output-0"
            )

class XAINode(BaseNodeHandler):
    """
    Call xAI API for text and media generation.
    Supports Grok models including image and video generation.
    """
    
    node_type = "xai"
    name = "xAI (Grok)"
    category = NodeCategory.AI.value
    description = "Generate text, images, and video using xAI Grok models"
    icon = "𝕏"
    color = "#000000"
    static_output_fields = ["content", "model", "media_url"]
    
    fields = [
        FieldConfig(
            name="credential",
            label="xAI API Key",
            field_type=FieldType.CREDENTIAL,
            credential_type="xai-api",
            description="Select your xAI credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=[],  # Dynamic
            default="grok-beta",
            description="Select an xAI model"
        ),
        FieldConfig(
            name="prompt",
            label="Prompt",
            field_type=FieldType.STRING,
            placeholder="Enter your prompt or generation request...",
            description="The prompt to send to Grok"
        ),
        FieldConfig(
            name="temperature",
            label="Temperature",
            field_type=FieldType.NUMBER,
            default=0.7,
            required=False,
            description="Creativity (0-2)"
        ),
        FieldConfig(
            name="max_tokens",
            label="Max Tokens",
            field_type=FieldType.NUMBER,
            default=2048,
            required=False,
            description="Maximum tokens to generate"
        ),
        FieldConfig(
            name="thinking",
            label="Show Reasoning (Thinking)",
            field_type=FieldType.BOOLEAN,
            default=False,
            required=False,
            description="If enabled, captures internal reasoning."
        ),
    ]
    
    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

    def get_dynamic_fields(self) -> dict[str, dict[str, Any]]:
        """Fetch xAI models from database"""
        try:
            from nodes.models import AIModel
            models = AIModel.objects.filter(provider__slug="xai", is_active=True).values_list('value', flat=True)
            options = list(models)
            return {
                "model": {
                    "options": options,
                    "defaultValue": "grok-beta" if "grok-beta" in options else (options[0] if options else "grok-beta")
                }
            }
        except Exception as e:
            logger.warning(f"Failed to fetch dynamic models for xAI: {e}")
            return {}

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        model = config.get("model", "grok-beta")
        prompt = config.get("prompt", "")
        temperature = float(config.get("temperature", 0.7))
        max_tokens = int(config.get("max_tokens", 2048))
        show_thinking = config.get("thinking", False)
        credential_id = config.get("credential")
        
        if not prompt:
            yield {"type": "error", "message": "Prompt is required"}
            return
            
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None
        
        if not api_key:
            yield {"type": "error", "message": "xAI API key not configured"}
            return
            
        try:
            is_gen = is_image_generation_model(model)
            if is_gen:
                res = await self.execute(input_data, config, context)
                if res.success:
                    data = res.get_data()
                    yield {"type": "content", "content": data.get("content", "")}
                    if data.get("media_url"):
                        yield {"type": "metadata", "media_url": data["media_url"]}
                else:
                    yield {"type": "error", "message": res.error}
                return

            attachments = config.get("attachments", [])
            user_msg_content = [{"type": "text", "text": prompt}]
            
            if attachments:
                import base64
                for att in attachments:
                    if att.file_type != 'image': continue
                    try:
                        file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                        with open(file_path, "rb") as f:
                            b64_data = base64.b64encode(f.read()).decode('utf-8')
                        user_msg_content.append({
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}
                        })
                    except: pass

            messages = []
            history = config.get("history", [])
            if history: messages.extend(history)
            
            payload = {
                "model": model,
                "messages": messages + [{"role": "user", "content": user_msg_content if len(user_msg_content) > 1 else prompt}],
                "temperature": temperature,
                "max_tokens": max_tokens,
                "stream": True
            }

            tools_payload = config.get("tools")
            enable_tools_ui = config.get("enable_tools", False)
            if not tools_payload and enable_tools_ui:
                import chat.tools as shared_tools
                tools_payload = shared_tools.AVAILABLE_TOOLS

            if tools_payload:
                payload["tools"] = tools_payload

            async with httpx.AsyncClient(timeout=120) as client:
                in_thinking = False
                async with client.stream(
                    "POST", 
                    "https://api.x.ai/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                ) as response:
                    if response.status_code != 200:
                        yield {"type": "error", "message": f"xAI API error: {response.status_code}"}
                        return

                    async for line in response.aiter_lines():
                        if not line.startswith("data: "): continue
                        data_str = line[6:].strip()
                        if not data_str or data_str == "[DONE]": continue
                        
                        try:
                            import json
                            chunk = json.loads(data_str)
                            choice = chunk.get("choices", [{}])[0]
                            delta = choice.get("delta", {})
                            
                            if "reasoning_content" in delta and delta["reasoning_content"]:
                                yield {"type": "thinking", "content": delta["reasoning_content"]}
                                continue

                            if "tool_calls" in delta:
                                yield {"type": "tool_calls", "tool_calls": delta["tool_calls"]}
                                continue

                            if "content" in delta and delta["content"]:
                                text = delta["content"]
                                if "<think>" in text:
                                    in_thinking = True
                                    parts = text.split("<think>", 1)
                                    if parts[0]: yield {"type": "content", "content": parts[0]}
                                    text = parts[1]
                                
                                if in_thinking:
                                    if "</think>" in text:
                                        parts = text.split("</think>", 1)
                                        yield {"type": "thinking", "content": parts[0]}
                                        in_thinking = False
                                        if parts[1]: yield {"type": "content", "content": parts[1]}
                                    else:
                                        yield {"type": "thinking", "content": text}
                                else:
                                    yield {"type": "content", "content": text}

                        except: continue

        except Exception as e:
            yield {"type": "error", "message": str(e)}
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        model = config.get("model", "grok-beta")
        prompt = config.get("prompt", "")
        temperature = float(config.get("temperature", 0.7))
        max_tokens = int(config.get("max_tokens", 2048))
        show_thinking = config.get("thinking", False)
        credential_id = config.get("credential")
        
        if not prompt:
            return NodeExecutionResult(success=False, error="Prompt is required", output_handle="output-0")
            
        creds = await context.get_credential(credential_id) if credential_id else None
        api_key = (creds.get("apiKey") or creds.get("api_key")) if creds else None
        
        if not api_key:
            return NodeExecutionResult(success=False, error="xAI API key not configured", output_handle="output-0")
            
        try:
            # Detect if this is an image/video generation model
            is_gen = is_image_generation_model(model)
            
            # xAI uses /v1/images/generations for image models
            endpoint = "https://api.x.ai/v1/images/generations" if is_gen else "https://api.x.ai/v1/chat/completions"
            
            payload = {}
            if is_gen:
                payload = {
                    "model": model,
                    "prompt": prompt,
                    "n": 1,
                    "size": config.get("size", "1024x1024"),
                }
            else:
                attachments = config.get("attachments", [])
                user_msg_content = [{"type": "text", "text": prompt}]
                
                if attachments:
                    import base64
                    for att in attachments:
                        try:
                            # Skip unsupported types for xAI (Grok usually supports images)
                            if att.file_type != 'image':
                                logger.info(f"Skipping unsupported attachment type {att.file_type} for xAI model {model}")
                                continue
                                
                            file_path = att.file.path if hasattr(att.file, 'path') else att.file.name
                            if not _validate_attachment_path(file_path):
                                logger.warning(f"Blocked path traversal in Gemini attachment")
                                continue
                            with open(file_path, "rb") as f:
                                b64_data = base64.b64encode(f.read()).decode('utf-8')
                            
                            user_msg_content.append({
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64_data}"}
                            })
                        except Exception as e:
                            logger.error(f"Failed to attach file {att.filename} to xAI request: {e}")

                messages = []
                history = config.get("history", [])
                if history:
                    messages.extend(history)

                payload = {
                    "model": model,
                    "messages": messages + [{"role": "user", "content": user_msg_content if len(user_msg_content) > 1 else prompt}],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }
            
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    endpoint,
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json"
                    },
                    json=payload
                )
                
                if response.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"xAI API error: {response.text}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                content = ""
                media_url = None
                
                if is_gen:
                    # Parse image generation response
                    image_data = data.get("data", [{}])[0]
                    media_url = image_data.get("url") or f"data:image/png;base64,{image_data.get('b64_json')}"
                    content = "Image generated successfully."
                else:
                    content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                
                result_data = {
                    "content": content,
                    "model": model,
                    "media_url": media_url,
                }
                
                # Simple thinking extraction for xAI
                captured_thinking = None
                if show_thinking:
                    import re
                    match = re.search(r"<think>(.*?)</think>", content, re.DOTALL)
                    if match:
                        captured_thinking = match.group(1).strip()
                        # Also remove think tags from content if we want clean output
                        content = re.sub(r"<think>.*?</think>", "", content, flags=re.DOTALL).strip()
                        result_data["content"] = content
                
                if captured_thinking:
                    result_data["thinking"] = captured_thinking

                return NodeExecutionResult(
                    success=True,
                    items=[NodeItem(json=result_data)],
                    output_handle="output-0"
                )
                
        except Exception as e:
            return NodeExecutionResult(success=False, error=f"xAI error: {str(e)}", output_handle="output-0")
