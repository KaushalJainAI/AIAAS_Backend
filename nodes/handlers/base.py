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


class NodeExecutionResult(BaseModel):
    """Result of a node execution"""
    success: bool = True
    data: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    output_handle: str = "output"  # Which output handle to use


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
        """
        # This is a placeholder logic. Real implementation needs to check parent's remaining time.
        # parent_timeout = getattr(parent_context, 'timeout_budget_ms', 300000)
        # child_timeout = child_workflow.workflow_settings.get('timeout', 60000)
        return 60000 # Default to 60s for now

    def _create_child_context(self, parent_context: 'ExecutionContext', child_workflow: Any, config: dict) -> Any:
        """
        Create an isolated execution context for the child workflow.
        Returns a new ExecutionContext object.
        """
        # This requires the ExecutionContext class definition which might cause circular imports
        # if imported at top level. 
        # For now, we stub this or expect the caller/subclass to handle the actual instantiation
        # using the parent class's type or a factory.
        pass
