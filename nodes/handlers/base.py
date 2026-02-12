"""
Base Node Handler with Pydantic Models

Core abstractions for the node system using Pydantic for type validation
and LangGraph compatibility.
"""
from abc import ABC, abstractmethod
from enum import Enum
from typing import Any, TYPE_CHECKING
from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from compiler.schemas import ExecutionContext


# ==================== Enums ====================

class FieldType(str, Enum):
    """Supported field types for node configuration"""
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"
    SELECT = "select"
    MULTI_SELECT = "multi_select"
    JSON = "json"
    CODE = "code"
    CREDENTIAL = "credential"
    FILE = "file"


class NodeCategory(str, Enum):
    """Node categories for organizing in palette"""
    TRIGGER = "trigger"
    ACTION = "action"
    TRANSFORM = "transform"
    CONDITIONAL = "conditional"
    INTEGRATION = "integration"
    AI = "ai"
    UTILITY = "utility"


# ==================== Pydantic Models ====================

class FieldConfig(BaseModel):
    """Configuration for a node input field"""
    name: str = Field(..., description="Field identifier", serialization_alias="id")
    label: str = Field(..., description="Display label")
    field_type: FieldType = Field(..., description="Type of field", serialization_alias="type")
    required: bool = Field(default=True, description="Whether field is required")
    default: Any = Field(default=None, description="Default value", serialization_alias="defaultValue")
    options: list[str] | None = Field(default=None, description="Options for SELECT type")
    placeholder: str = Field(default="", description="Placeholder text")
    description: str = Field(default="", description="Help text")
    credential_type: str | None = Field(default=None, description="Detailed type for CREDENTIAL field", serialization_alias="credentialType")
    
    model_config = {"use_enum_values": True, "populate_by_name": True}


class HandleDef(BaseModel):
    """Definition for an input/output handle"""
    id: str = Field(..., description="Handle identifier")
    label: str = Field(default="", description="Display label")
    handle_type: str = Field(default="default", description="Handle type: default, success, error")
    
    model_config = {"frozen": True}


class NodeSchema(BaseModel):
    """Schema returned to frontend for node palette"""
    node_type: str = Field(..., serialization_alias="nodeType")
    name: str = Field(..., serialization_alias="displayName")
    category: str
    description: str
    icon: str
    color: str
    fields: list[FieldConfig]
    inputs: list[HandleDef]
    outputs: list[HandleDef]
    
    model_config = {"use_enum_values": True, "populate_by_name": True}


class NodeItem(BaseModel):
    """
    Single item in node output - n8n compatible.
    
    Each item represents one unit of data flowing through the workflow.
    The `json` key holds the primary data, while `binary` holds file data.
    """
    json_data: dict[str, Any] = Field(
        default_factory=dict, 
        description="Primary data payload",
        alias="json",
    )
    binary: dict[str, Any] | None = Field(default=None, description="Binary file data (base64)")
    pairedItem: dict[str, int] | None = Field(default=None, description="Link to source item for traceability")
    
    model_config = {
        "extra": "allow", 
        "populate_by_name": True,
        "serialize_by_alias": True  # Always serialize as 'json' not 'json_data'
    }
    
    @property
    def json(self) -> dict[str, Any]:
        """Accessor for json data (n8n compatibility)."""
        return self.json_data


class NodeExecutionResult(BaseModel):
    """
    Result of a node execution - n8n style.
    
    All node outputs are standardized as an array of items.
    Each item contains a `json` key with the data payload.
    
    For backward compatibility, you can still pass `data=` and it will be auto-converted to items.
    """
    success: bool = True
    items: list[NodeItem] = Field(default_factory=list, description="Output items array")
    data: dict[str, Any] | None = Field(default=None, exclude=True, description="DEPRECATED: Use items instead")
    error: str | None = None
    output_handle: str = "output"  # Which output handle to use
    
    def __init__(self, **data):
        """Handle backward compatibility with old 'data' field."""
        # If data is provided but items is not, convert data to items
        if 'data' in data and data['data'] is not None and 'items' not in data:
            legacy_data = data.pop('data')
            data['items'] = [NodeItem(json=legacy_data)]
        elif 'data' in data:
            data.pop('data', None)  # Remove data if items also provided
        super().__init__(**data)
    
    @classmethod
    def from_data(cls, data: dict[str, Any], success: bool = True) -> "NodeExecutionResult":
        """Helper to create result from a single data dict."""
        return cls(success=success, items=[NodeItem(json=data)])
    
    @classmethod
    def from_items_list(cls, items: list[dict], success: bool = True) -> "NodeExecutionResult":
        """Helper to create result from list of raw dicts."""
        return cls(success=success, items=[NodeItem(json=item) for item in items])
    
    def get_data(self) -> dict[str, Any]:
        """Backward compatibility: get first item's json data."""
        if self.items:
            return self.items[0].json
        return {}
    
    def get_all_json(self) -> list[dict[str, Any]]:
        """Get all items' json data as a list."""
        return [item.json for item in self.items]


# ==================== Base Handler ====================

class BaseNodeHandler(ABC):
    """
    Abstract base class for all node handlers.
    
    Each node type must implement this class and define:
    - node_type: Unique identifier
    - name: Display name
    - category: NodeCategory enum value
    - fields: List of FieldConfig for configuration
    - execute: Async method to run the node
    """
    
    # Class attributes - override in subclasses
    node_type: str = ""
    name: str = ""
    category: str = NodeCategory.ACTION.value
    description: str = ""
    icon: str = "⚡"
    color: str = "#6366f1"
    
    # Default single input/output
    fields: list[FieldConfig] = []
    inputs: list[HandleDef] = [HandleDef(id="input")]
    outputs: list[HandleDef] = [HandleDef(id="output")]
    
    @abstractmethod
    async def execute(
        self,
        input_data: dict[str, Any],
        config: dict[str, Any],
        context: 'ExecutionContext'
    ) -> NodeExecutionResult:
        """
        Execute the node logic.
        
        Args:
            input_data: Data received from connected upstream nodes
            config: Node configuration (field values set by user)
            context: Execution context with credentials, variables, etc.
        
        Returns:
            NodeExecutionResult with success status and output data
        """
        pass
    
    async def poll(
        self,
        config: dict[str, Any],
        state: dict[str, Any],
        context: 'ExecutionContext'
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """
        Poll for new items.
        
        This should be implemented by triggers that pull data (Email, RSS, etc.).
        
        Args:
            config: Node configuration
            state: Persistent trigger state (cursor)
            context: Orchestration context
            
        Returns:
            tuple (list_of_new_items, updated_state)
        """
        return [], state
    
    def validate_config(self, config: dict[str, Any]) -> list[str]:
        """
        Validate node configuration.
        
        Returns list of error messages, empty if valid.
        """
        errors = []
        
        for field in self.fields:
            value = config.get(field.name)
            
            if field.required and value is None:
                errors.append(f"Field '{field.label}' is required")
            
            if field.field_type == FieldType.SELECT and value:
                if field.options and value not in field.options:
                    errors.append(f"Invalid option '{value}' for field '{field.label}'")
        
        return errors
    
    def get_schema(self) -> NodeSchema:
        """Generate schema for frontend"""
        return NodeSchema(
            node_type=self.node_type,
            name=self.name,
            category=self.category,
            description=self.description,
            icon=self.icon,
            color=self.color,
            fields=self.fields,
            inputs=self.inputs,
            outputs=self.outputs,
        )
    
    def __repr__(self) -> str:
        return f"<{self.__class__.__name__}: {self.node_type}>"

    def _detect_circular_dependency(self, workflow_id: str, context: 'ExecutionContext') -> bool:
        """
        Check if workflow_id is already in the execution chain to prevent infinite recursion.
        """
        # Assuming context has a 'workflow_chain' attribute which is a list of workflow IDs
        if not hasattr(context, 'workflow_chain'):
            return False
        return workflow_id in context.workflow_chain

    def _transform_state(self, data: dict, mapping: dict) -> dict:
        """
        Transform state data based on a mapping configuration.
        Mapping format: {'target_field': 'source_field_path'}
        """
        if not mapping:
            return data
            
        transformed = {}
        for target, source in mapping.items():
            # Simple direct mapping for now. 
            # In production, this would handle dot notation for nested fields e.g. "body.user.id"
            if source in data:
                transformed[target] = data[source]
            # Handle deep get if source has dots, etc.
        
        return transformed

    def _calculate_timeout(self, child_workflow: Any, parent_context: 'ExecutionContext') -> int:
        """
        Calculate timeout budget for child workflow.
        
        Uses the minimum of:
        - Parent's remaining timeout budget
        - Child workflow's configured timeout
        - Default timeout of 60 seconds
        """
        default_timeout = 60000  # 60s default
        
        # Get parent's remaining time budget
        parent_remaining = getattr(parent_context, 'timeout_budget_ms', default_timeout)
        
        # Get child workflow's configured timeout if available
        child_timeout = default_timeout
        if hasattr(child_workflow, 'workflow_settings'):
            child_timeout = child_workflow.workflow_settings.get('timeout', default_timeout)
        elif isinstance(child_workflow, dict):
            child_timeout = child_workflow.get('settings', {}).get('timeout', default_timeout)
        
        # Return the minimum to ensure we don't exceed parent's budget
        return min(parent_remaining, child_timeout, default_timeout)

    def _create_child_context(self, parent_context: 'ExecutionContext', child_workflow: Any, config: dict) -> 'ExecutionContext':
        """
        Create an isolated execution context for the child workflow.
        
        Inherits credentials and variables from parent, but creates isolated
        state for the child execution.
        """
        # Import here to avoid circular imports at module level
        from compiler.schemas import ExecutionContext
        
        # Track nesting depth
        current_depth = getattr(parent_context, 'nesting_depth', 0)
        max_depth = getattr(parent_context, 'max_nesting_depth', 3)
        
        # Get workflow_id from child_workflow
        workflow_id = None
        if hasattr(child_workflow, 'id'):
            workflow_id = str(child_workflow.id)
        elif isinstance(child_workflow, dict):
            workflow_id = str(child_workflow.get('id', 'unknown'))
        
        # Build workflow chain for circular dependency detection
        parent_chain = getattr(parent_context, 'workflow_chain', [])
        new_chain = parent_chain + [workflow_id] if workflow_id else parent_chain
        
        # Calculate timeout for child
        timeout = self._calculate_timeout(child_workflow, parent_context)
        
        # Create child context with inherited but isolated state
        child_context = ExecutionContext(
            workflow_id=workflow_id,
            execution_id=f"{parent_context.execution_id}_sub_{current_depth + 1}",
            credentials=parent_context.credentials.copy() if parent_context.credentials else {},
            variables=parent_context.variables.copy() if parent_context.variables else {},
            node_outputs={},  # Fresh outputs for child
            loop_stats={},    # Fresh loop stats
            current_node_id=None,
        )
        
        # Set child-specific attributes
        child_context.nesting_depth = current_depth + 1
        child_context.max_nesting_depth = max_depth
        child_context.workflow_chain = new_chain
        child_context.timeout_budget_ms = timeout
        child_context.parent_context = parent_context
        
        return child_context

