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
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
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
                output_handle="error"
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
                        output_handle="error"
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
                    output_handle="success"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="OpenAI API request timed out",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"OpenAI error: {str(e)}",
                output_handle="error"
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
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
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
                output_handle="error"
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
                        output_handle="error"
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
                    output_handle="success"
                )
                
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Gemini API request timed out",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Gemini error: {str(e)}",
                output_handle="error"
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
        HandleDef(id="success", label="Success", handle_type="success"),
        HandleDef(id="error", label="Error", handle_type="error"),
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
                        output_handle="error"
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
                    output_handle="success"
                )
                
        except httpx.ConnectError:
            return NodeExecutionResult(
                success=False,
                error=f"Cannot connect to Ollama at {base_url}. Is Ollama running?",
                output_handle="error"
            )
        except httpx.TimeoutException:
            return NodeExecutionResult(
                success=False,
                error="Ollama request timed out (model may be loading)",
                output_handle="error"
            )
        except Exception as e:
            return NodeExecutionResult(
                success=False,
                error=f"Ollama error: {str(e)}",
                output_handle="error"
            )
