"""
LLM Node Handlers

AI/LLM nodes for OpenAI, Google Gemini, and local Ollama integration.
"""
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
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="OpenAI API key not configured",
                output_handle="output-0"
            )
        
        api_key = creds["api_key"]
        
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    "https://api.openai.com/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_message},
                            {"role": "user", "content": prompt},
                        ],
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                    },
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
            credential_type="gemini_api",
            description="Select your Google AI credential"
        ),
        FieldConfig(
            name="model",
            label="Model",
            field_type=FieldType.SELECT,
            options=["gemini-2.0-flash-exp", "gemini-1.5-flash", "gemini-1.5-pro"],
            default="gemini-1.5-flash"
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
        credential_id = config.get("credential")
        model = config.get("model", "gemini-1.5-flash")
        prompt = config.get("prompt", "")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 1024)
        
        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="error"
            )
        
        # Get API key from credentials
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Gemini API key not configured",
                output_handle="output-0"
            )
        
        api_key = creds["api_key"]
        
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                    params={"key": api_key},
                    headers={"Content-Type": "application/json"},
                    json={
                        "contents": [{"parts": [{"text": prompt}]}],
                        "generationConfig": {
                            "temperature": temperature,
                            "maxOutputTokens": max_tokens,
                        },
                    },
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
                        "model": model,
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
            async with httpx.AsyncClient(timeout=300) as client:
                # Build messages
                messages = []
                if system_message:
                    messages.append({"role": "system", "content": system_message})
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
            credential_type="perplexity_api",
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
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="Perplexity API key not configured",
                output_handle="output-0"
            )
        
        api_key = creds["api_key"]
        
        try:
            # Build request payload
            messages = [
                {"role": "system", "content": system_message},
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
    color = "#6366f1"  # Indigo
    
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
            options=[
                # Popular Free Models
                "google/gemini-2.0-flash-exp:free",
                "meta-llama/llama-3.3-70b-instruct:free",
                "qwen/qwen-2.5-72b-instruct:free",
                "deepseek/deepseek-chat:free",
                "mistralai/mistral-small-24b-instruct-2501:free",
                # OpenAI
                "openai/gpt-4o",
                "openai/gpt-4o-mini",
                "openai/gpt-4-turbo",
                "openai/o1",
                "openai/o1-mini",
                # Anthropic
                "anthropic/claude-3.5-sonnet",
                "anthropic/claude-3.5-haiku",
                "anthropic/claude-3-opus",
                # Google
                "google/gemini-2.0-flash-001",
                "google/gemini-pro-1.5",
                "google/gemini-flash-1.5",
                # Meta Llama
                "meta-llama/llama-3.3-70b-instruct",
                "meta-llama/llama-3.1-405b-instruct",
                "meta-llama/llama-3.1-70b-instruct",
                # Mistral
                "mistralai/mistral-large-2411",
                "mistralai/mistral-medium",
                "mistralai/mixtral-8x22b-instruct",
                # DeepSeek
                "deepseek/deepseek-r1",
                "deepseek/deepseek-chat",
                # Qwen
                "qwen/qwen-2.5-72b-instruct",
                "qwen/qwq-32b",
                # Cohere
                "cohere/command-r-plus",
                "cohere/command-r",
            ],
            default="google/gemini-2.0-flash-exp:free"
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
        model = config.get("model", "google/gemini-2.0-flash-exp:free")
        prompt = config.get("prompt", "")
        system_message = config.get("system_message", "You are a helpful assistant.")
        temperature = config.get("temperature", 0.7)
        max_tokens = config.get("max_tokens", 2048)
        top_p = config.get("top_p", 1.0)
        
        if not prompt:
            return NodeExecutionResult(
                success=False,
                error="Prompt is required",
                output_handle="error"
            )
        
        # Get API key from credentials
        creds = context.get_credential(credential_id) if credential_id else None
        if not creds or "api_key" not in creds:
            return NodeExecutionResult(
                success=False,
                error="OpenRouter API key not configured",
                output_handle="output-0"
            )
        
        api_key = creds["api_key"]
        
        try:
            # Build messages
            messages = []
            if system_message:
                messages.append({"role": "system", "content": system_message})
            messages.append({"role": "user", "content": prompt})
            
            async with httpx.AsyncClient(timeout=180) as client:
                response = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "HTTP-Referer": "https://aiaas.local",  # Optional: your app URL
                        "X-Title": "AIAAS Workflow",  # Optional: your app name
                    },
                    json={
                        "model": model,
                        "messages": messages,
                        "temperature": temperature,
                        "max_tokens": max_tokens,
                        "top_p": top_p,
                    },
                )
                
                if response.status_code != 200:
                    error_data = response.json() if response.headers.get("content-type", "").startswith("application/json") else {}
                    error_msg = error_data.get("error", {}).get("message", response.text) if isinstance(error_data.get("error"), dict) else str(error_data.get("error", response.text))
                    return NodeExecutionResult(
                        success=False,
                        error=f"OpenRouter API error: {error_msg}",
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
                error="OpenRouter API request timed out",
                output_handle="output-0"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"OpenRouter error: {str(e)}",
                output_handle="output-0"
            )
