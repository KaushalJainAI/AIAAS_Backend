"""
Orchestrator Context Management

Separates design-time context (for workflow generation) from
runtime context (for workflow execution control).

Design Principle:
- Design-time context uses knowledge base (templates, patterns)
- Runtime context uses dynamic state (outputs, goals, conditions)
"""
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID
from datetime import datetime


@dataclass
class RuntimeContext:
    """
    Runtime context for workflow execution control.
    
    This is the ONLY context used during execution.
    It does NOT use the knowledge base or templates.
    
    All decisions are based on:
    - Node outputs at runtime
    - The execution goal
    - Current state and conditions
    """
    
    execution_id: UUID
    goal: str = ""  # What we're trying to achieve (e.g., "Send report if data > 100 rows")
    goal_conditions: dict = field(default_factory=dict)  # {"min_rows": 100, "max_errors": 5}
    
    # Runtime state
    current_node: str = ""
    node_outputs: dict[str, Any] = field(default_factory=dict)  # node_id -> output
    runtime_variables: dict[str, Any] = field(default_factory=dict)
    
    # Error tracking
    errors: list[dict] = field(default_factory=list)  # [{node_id, error, timestamp}]
    
    # HITL decisions made during this execution
    hitl_decisions: list[dict] = field(default_factory=list)  # [{question, response, timestamp}]
    
    # Timing
    started_at: datetime = field(default_factory=datetime.utcnow)
    
    def record_output(self, node_id: str, output: Any) -> None:
        """Store a node's output for goal checking."""
        self.node_outputs[node_id] = output
    
    def record_error(self, node_id: str, error: str) -> None:
        """Track an error for decision making."""
        self.errors.append({
            "node_id": node_id,
            "error": error,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    def record_hitl_decision(self, question: str, response: Any) -> None:
        """Track human decisions made during execution."""
        self.hitl_decisions.append({
            "question": question,
            "response": response,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    def get_last_output(self) -> Any:
        """Get the most recent node output."""
        if self.current_node and self.current_node in self.node_outputs:
            return self.node_outputs[self.current_node]
        return None
    
    def check_goal_condition(self, condition_name: str, value: Any) -> bool:
        """
        Check if a runtime value meets a goal condition.
        
        Example:
            ctx.goal_conditions = {"min_rows": 100}
            ctx.check_goal_condition("min_rows", len(data))  # True if len(data) >= 100
        """
        if condition_name not in self.goal_conditions:
            return True  # No condition = pass
        
        threshold = self.goal_conditions[condition_name]
        
        if condition_name.startswith("min_"):
            return value >= threshold
        elif condition_name.startswith("max_"):
            return value <= threshold
        elif condition_name.startswith("equals_"):
            return value == threshold
        else:
            return value >= threshold  # Default: treat as minimum
    
    def should_continue_for_goal(self, output: Any) -> tuple[bool, str]:
        """
        Determine if we should continue based on goal and output.
        
        Returns (should_continue, reason)
        """
        # Check row count conditions
        if "min_rows" in self.goal_conditions:
            if isinstance(output, (list, tuple)):
                row_count = len(output)
                if row_count < self.goal_conditions["min_rows"]:
                    return False, f"Data has {row_count} rows, minimum is {self.goal_conditions['min_rows']}"
        
        # Check error thresholds
        if "max_errors" in self.goal_conditions:
            if len(self.errors) > self.goal_conditions["max_errors"]:
                return False, f"Error count ({len(self.errors)}) exceeds maximum ({self.goal_conditions['max_errors']})"
        
        # Check for explicit stop conditions in output
        if isinstance(output, dict):
            if output.get("should_stop"):
                return False, output.get("stop_reason", "Node requested stop")
            if output.get("skip_remaining"):
                return False, "Skip remaining nodes"
        
        return True, "Continue"
    
    @property
    def error_count(self) -> int:
        """Number of errors so far."""
        return len(self.errors)
    
    @property
    def has_errors(self) -> bool:
        """Whether any errors occurred."""
        return len(self.errors) > 0


@dataclass
class DesignTimeContext:
    """
    Design-time context for workflow generation and modification.
    
    This uses the knowledge base (templates, patterns) but is
    NEVER used during workflow execution.
    
    Used for:
    - Generating new workflows from natural language
    - Modifying existing workflows
    - Suggesting improvements
    - Finding similar templates
    """
    
    user_id: int
    conversation_history: list[dict] = field(default_factory=list)
    user_preferences: dict = field(default_factory=dict)
    
    # These are populated lazily when needed
    _template_results: list = field(default_factory=list)
    
    def add_message(self, role: str, content: str) -> None:
        """Add a message to conversation history."""
        self.conversation_history.append({
            "role": role,
            "content": content,
            "timestamp": datetime.utcnow().isoformat()
        })
    
    def get_recent_messages(self, limit: int = 10) -> list[dict]:
        """Get the most recent conversation messages."""
        return self.conversation_history[-limit:]
    
    def format_for_prompt(self) -> str:
        """Format context for inclusion in LLM prompt."""
        parts = []
        
        if self.user_preferences:
            prefs = ", ".join(f"{k}: {v}" for k, v in self.user_preferences.items())
            parts.append(f"[USER PREFERENCES]\n{prefs}")
        
        if self.conversation_history:
            recent = self.get_recent_messages(5)
            history = "\n".join(f"{m['role']}: {m['content']}" for m in recent)
            parts.append(f"[RECENT CONVERSATION]\n{history}")
        
        return "\n\n".join(parts)
