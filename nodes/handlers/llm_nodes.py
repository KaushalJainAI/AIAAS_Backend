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
)

logger = logging.getLogger(__name__)

def format_skills_as_context(skills: list[dict]) -> str:
    """Format skill list into a context block for LLM prompts."""
    if not skills:
        return ""
    
    parts = ["\n[CONTEXT / SKILLS]"]
    for s in skills:
        parts.append(f"### {s['title']}\n{s['content']}")
    parts.append("[END CONTEXT]\n")
    return "\n".join(parts)


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
            options=["gpt-4o", "gpt-4o-mini", "gpt-4-turbo", "gpt-4", "gpt-3.5-turbo"],
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
        model = config.get("model", "gpt-4o-mini")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 1024)
        
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
                payload = {
                    "model": model,
                    "messages": [
                        {"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"},
                        {"role": "user", "content": prompt},
                    ],
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                }

                # Dynamically add custom parameters from the config
                for key, value in config.items():
                    if key.startswith("custom_") and value is not None:
                        param_name = key.replace("custom_", "")
                        # Try to parse numeric or boolean values if they came as strings from expressions
                        payload[param_name] = value

                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers=headers,
                    json=payload,
                )
                
                if response.status_code != 200:
                    error_data = response.json()
                    error_msg = error_data.get("error", {}).get("message", response.text)
                    return NodeExecutionResult(
                        success=False,
                        error=f"OpenAI API error: {error_msg}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                content = data["choices"][0]["message"]["content"]
                usage = data.get("usage", {})
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "content": content,
                        "model": model,
                        "usage": {
                            "prompt_tokens": usage.get("prompt_tokens", 0),
                            "completion_tokens": usage.get("completion_tokens", 0),
                            "total_tokens": usage.get("total_tokens", 0),
                        },
                        "input": input_data,
                    },
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
    name = "Gemini"
    category = NodeCategory.AI.value
    description = "Generate text using Google Gemini models"
    icon = "✨"
    color = "#4285f4"  # Google blue
    
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
            options=[
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-2.0-flash", 
                "gemini-2.0-flash-lite", 
                "gemini-1.5-pro", 
                "gemini-1.5-flash",
            ],
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
        # 1. Fetch Configuration
        model_name = config.get("model", "gemini-1.5-flash")
        user_prompt = config.get("prompt", "")
        sys_message = config.get("system_message", "You are a helpful assistant.")
        temp = config.get("temperature", 0.7)
        tokens_limit = config.get("max_tokens", 1024)
        
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

                # Add custom parameters from config
                for key, value in config.items():
                    if key.startswith("custom_") and value is not None:
                        param_name = key.replace("custom_", "")
                        if param_name == "top_p":
                            generation_config["topP"] = value
                        elif param_name == "top_k":
                            generation_config["topK"] = value
                        else:
                            generation_config[param_name] = value

                # Build compatible payload by merging system message into prompt
                # (Avoids 'Unknown field system_instruction' on certain API versions/models)
                full_prompt = f"{user_prompt}"
                if sys_message or skills_context:
                    header = f"[SYSTEM INSTRUCTION]\n{sys_message}\n{skills_context}\n\n[USER PROMPT]\n"
                    full_prompt = f"{header}{user_prompt}"

                payload = {
                    "contents": [{"parts": [{"text": full_prompt}]}],
                    "generationConfig": generation_config,
                }

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
                content = data["candidates"][0]["content"]["parts"][0]["text"]
                usage = data.get("usageMetadata", {})
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "content": content,
                        "model": model_name,
                        "usage": {
                            "prompt_tokens": usage.get("promptTokenCount", 0),
                            "completion_tokens": usage.get("candidatesTokenCount", 0),
                            "total_tokens": usage.get("totalTokenCount", 0),
                        },
                        "input": input_data,
                    },
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
    name = "Ollama (Local)"
    category = NodeCategory.AI.value
    description = "Generate text using local Ollama models"
    icon = "🦙"
    color = "#0d1117"  # Ollama dark
    
    fields = [
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=["llama3.2", "llama3.1", "mistral", "codellama", "phi3", "gemma2"],
            default="llama3.2"
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
                messages = []
                if system_message:
                    messages.append({"role": "system", "content": f"{system_message}{format_skills_as_context(all_skills)}"})
                elif all_skills:
                    messages.append({"role": "system", "content": format_skills_as_context(all_skills)})
                messages.append({"role": "user", "content": prompt})
                
                response = await client.post(
                    f"{base_url.rstrip('/')}/api/chat",
                    json={
                        "model": model,
                        "messages": messages,
                        "stream": False,
                        "options": {
                            "temperature": temperature,
                        },
                    },
                )
                
                if response.status_code != 200:
                    return NodeExecutionResult(
                        success=False,
                        error=f"Ollama error: {response.text}",
                        output_handle="output-0"
                    )
                
                data = response.json()
                content = data.get("message", {}).get("content", "")
                
                return NodeExecutionResult(
                    success=True,
                    data={
                        "content": content,
                        "model": model,
                        "total_duration": data.get("total_duration", 0),
                        "eval_count": data.get("eval_count", 0),
                        "input": input_data,
                    },
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
    name = "Perplexity"
    category = NodeCategory.AI.value
    description = "Generate web-grounded answers using Perplexity AI"
    icon = "🔍"
    color = "#1FB1E6"  # Perplexity blue
    
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
            options=[
                "sonar",
                "sonar-pro", 
                "sonar-reasoning",
            ],
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
                {"role": "user", "content": prompt},
            ]
            
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
                
                return NodeExecutionResult(
                    success=True,
                    data=result_data,
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
    name = "OpenRouter"
    category = NodeCategory.AI.value
    description = "Access 100+ AI models through OpenRouter (GPT-4, Claude, Llama, Mistral, etc.)"
    icon = "🌐"
    color = "#6366f1"

    # Only include currently-live models with :free suffix
    # All of these are listed as free in Feb 2026 model trackers. [web:8][web:13][web:22]
    MODEL_OPTIONS = [
        # Routers
        "openrouter/auto",              # Smart routing (may use paid models) [web:39]
        "openrouter/free",              # Routes only across free models [web:18]

        # Meta Llama (Free)
        "meta-llama/llama-3.3-70b-instruct:free",
        "meta-llama/llama-3.1-405b-instruct:free",   # free tier variant [web:8][web:13]

        # Google / Gemma / Gemini (Free)
        "google/gemini-2.0-flash-exp:free",          # 1M ctx, free tier [web:8]
        "google/gemma-3-27b-it:free",
        "google/gemma-3-12b-it:free",
        "google/gemma-3-4b-it:free",                 # all listed as free [web:13]

        # DeepSeek (Free)
        "deepseek/deepseek-r1-0528:free",            # May 2028 update, free tier [web:8][web:13]
        "deepseek/deepseek-chat:free",

        # Qwen (Free)
        "qwen/qwen3-coder-480b:free",
        "qwen/qwen2.5-vl-7b-instruct:free",          # VL free tier [web:8][web:13]

        # Mistral (Free)
        "mistralai/mistral-small-3.1:free",
        "mistralai/mistral-7b-instruct:free",
        "mistralai/devstral-2512:free",              # Devstral 2 free coding model [web:8][web:25]

        # NVIDIA (Free)
        "nvidia/nemotron-3-nano-30b:free",
        "nvidia/nemotron-nano-vl-12b:free",          # VL free tier [web:8][web:13]

        # Arcee AI (Free)
        "arcee-ai/trinity-large:free",
        "arcee-ai/trinity-mini:free",                # both listed free [web:8][web:13]

        # Nous Research (Free)
        "nousresearch/hermes-3-405b:free",           # on free list [web:8]

        # StepFun (Free)
        "stepfun/step-3.5-flash:free",               # Step 3.5 Flash free [web:8][web:13]

        # Upstage (Free)
        "upstage/solar-pro-3:free",                  # Solar Pro 3 free [web:8][web:13]

        # Liquid AI (Free)
        "liquid-ai/lfm-2.5-1.2b-thinking:free",
        "liquid-ai/lfm-2.5-1.2b-instruct:free",      # both free [web:8]

        # Xiaomi / MiMo (Free)
        "xiaomi/mimo-v2-flash:free",                 # MiMo-V2-Flash free [web:8][web:25]

        # Z.AI / GLM (Free)
        "z-ai/glm-4.5-air:free",                     # GLM‑4.5 Air free [web:8]
    ]

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
            options=MODEL_OPTIONS,
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
            description="Maximum tokens in response"
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
            options=["text", "json_object"],
            default="text",
            required=False,
            description="Output format: plain text or forced JSON object"
        ),
    ]

    outputs = [
        HandleDef(id="output-0", label="Output"),
    ]

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

        response_format = config.get("response_format", "text")

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
            # ── Skills + Messages ─────────────────────────────────────────────
            all_skills    = await resolve_node_skills(config, context)
            skill_context = format_skills_as_context(all_skills)

            messages = [
                {"role": "system", "content": f"{system_message}{skill_context}"},
                {"role": "user",   "content": prompt},
            ]

            # ── Request Body ──────────────────────────────────────────────────
            body: dict[str, Any] = {
                "model":       model,
                "messages":    messages,
                "temperature": temperature,
                "max_tokens":  max_tokens,
                "top_p":       top_p,
            }

            if response_format == "json_object":
                body["response_format"] = {"type": "json_object"}

            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type":  "application/json",
                # These are recommended but optional headers. [web:33][web:39]
                "HTTP-Referer":  "https://aiaas.local",
                "X-Title":       "AIAAS Workflow",
            }

            # ── API Call (with 404 auto-fallback) ─────────────────────────────
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers=headers,
                    json=body,
                )

                # 404: model no longer available — retry with fallback
                if response.status_code == 404 and model != self.FALLBACK_MODEL:
                    body["model"] = self.FALLBACK_MODEL
                    response = await client.post(
                        "https://openrouter.ai/api/v1/chat/completions",
                        headers=headers,
                        json=body,
                    )
                    used_fallback = True
                    actual_model  = self.FALLBACK_MODEL
                else:
                    used_fallback = (model == self.FALLBACK_MODEL)
                    actual_model  = model

            # ── Non-200 after fallback attempt ────────────────────────────────
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
            if not choices:
                return NodeExecutionResult(
                    success=False,
                    error="OpenRouter API returned no choices",
                    output_handle="output-0"
                )

            choice        = choices[0]
            content       = choice.get("message", {}).get("content", "")
            finish_reason = choice.get("finish_reason", "unknown")
            generation_id = data.get("id", "")
            usage         = data.get("usage", {})

            # Extract reasoning/thinking if available (e.g. from DeepSeek R1)
            reasoning = choice.get("message", {}).get("reasoning", "")
            
            # Some providers might put it in 'thinking' or other fields
            if not reasoning:
                reasoning = choice.get("message", {}).get("thinking", "")

            return NodeExecutionResult(
                success=True,
                data={
                    "content":         content,
                    "reasoning":       reasoning,
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
                },
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

