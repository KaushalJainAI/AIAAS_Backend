"""
The LLM handler calling convention.

`BaseNodeHandler` is what every provider handler subclasses and what
`registry.get_handler()` returns on the agent hot path. The "node" vocabulary
in the names is historical: this was once the calling convention for the whole
workflow-canvas node system.

The DAG-era surface that used to sit here went with the runtime — the trigger
`poll()` hook, the `NodeSchema` / `get_schema()` / `validate_config()` palette
machinery — and the canvas presentation layer followed it: `FieldConfig` /
`FieldType` (properties-panel inputs), `HandleDef` (connection points),
`NodeCategory`, and the `NodeItem` array that let one node fan out over many
items. Nothing outside this package ever read them, and the `llm` app serves
exactly one view, so what is left is the part a model call actually needs:
config in, `NodeExecutionResult` out.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, TYPE_CHECKING

from pydantic import BaseModel, Field, field_validator

if TYPE_CHECKING:
    from llm.context import ExecutionContext


class NodeExecutionResult(BaseModel):
    """
    Standardised handler output: one call in, one payload out.

    This was an array of `NodeItem`s carrying n8n's `binary` and `pairedItem`
    traceability, because a DAG node could fan out over many items and a
    downstream node had to know which input produced which output. A handler
    now serves one model call: every construction site built a single-element
    list and every reader took `[0]`, so the array only ever cost an unwrap.

    It also accepted a legacy `data=` kwarg that a custom `__init__` folded
    into `items`, which `get_data()` then unfolded back out. `data` is the
    field again, and the round trip is gone.
    """
    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict, description="Output payload")
    error: str | None = None
    #: HTTP status behind `error`, when the failure came from an upstream API.
    #: Callers use it to tell an account problem (401/402) — which the user must
    #: fix and should be told about at once — from a transient one worth a retry.
    status_code: int | None = None

    @field_validator("data", mode="before")
    @classmethod
    def _none_to_empty(cls, v: Any) -> dict:
        return v if v is not None else {}


# ==================== Schema Helpers ====================

def build_json_schema_from_fields(fields: list[dict]) -> dict | None:
    """
    Convert user-defined custom field defs into a JSON Schema.

    Args:
        fields: List of dicts with 'id', 'type', 'label' keys.
               id is prefixed with 'custom_' (will be stripped).

    Returns:
        JSON Schema dict or None if no fields.
    """
    if not fields:
        return None
    type_map = {"text": "string", "number": "number", "boolean": "boolean", "json": "object"}
    props = {}
    for f in fields:
        field_name = f.get("id", "").replace("custom_", "")
        if not field_name:
            continue
        json_type = type_map.get(f.get("type", "text"), "string")
        props[field_name] = {"type": json_type}
    if not props:
        return None
    return {
        "type": "object",
        "properties": props,
        "required": list(props.keys()),
        "additionalProperties": False,
    }


def format_schema_for_prompt(schema: dict) -> str:
    """Format a JSON schema as a prompt instruction for models without native JSON mode."""
    if not schema:
        return ""
    fields_desc = []
    for name, prop in schema.get("properties", {}).items():
        fields_desc.append(f'  "{name}": <{prop["type"]}>')
    fields_str = ",\n".join(fields_desc)
    return (
        "\n\nIMPORTANT: You MUST respond with ONLY a valid JSON object matching this exact schema, "
        "with no extra text before or after:\n"
        "{\n" + fields_str + "\n}\n"
        "Do NOT include markdown code fences or any text outside the JSON."
    )


# ==================== Base Handler ====================

class BaseNodeHandler(ABC):
    """
    Abstract base class for all LLM provider handlers.

    Each provider implements this class and defines:
    - node_type: Unique identifier (the registry key, a provider slug)
    - name: Display name, for logs and error messages
    - execute: Async method to run the handler

    Handlers used to also declare `fields` (a list of `FieldConfig`), `inputs` /
    `outputs` (`HandleDef` connection points), `category`, `icon` and `color` —
    everything the workflow canvas needed to draw a node and render its
    properties panel. Nothing outside this package ever read them once the
    canvas was retired, and the `llm` app serves exactly one view, so they are
    gone along with `get_dynamic_fields`, the hook that filled the model
    dropdown of a panel that no longer renders.
    """

    # Class attributes - override in subclasses
    node_type: str = ""
    name: str = ""
    description: str = ""

    @abstractmethod
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        """
        Execute the handler logic.

        Args:
            input_data: Data received from upstream (unused by LLM handlers;
                        kept because the calling convention predates them)
            config: Handler configuration (field values set by the caller)
            context: Execution context — whose credentials to spend

        Returns:
            NodeExecutionResult with success status and output data
        """
        pass

    async def stream_execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ):
        """
        Stream response tokens if supported.
        Falling back to standard execute if not implemented.
        """
        result = await self.execute(input_data, config, context)
        if result.success:
            data = result.data
            yield {"type": "content", "content": data.get("content", "")}
            # Yield usage and other metadata in a final chunk
            final_meta = {k: v for k, v in data.items() if k != "content"}
            yield {"type": "metadata", **final_meta}
        else:
            yield {"type": "error", "message": result.error}

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.node_type}>"