import logging
import json
from typing import Dict, Any, List

logger = logging.getLogger(__name__)

class ToolExecutor:
    AVAILABLE_TOOLS = [
        {
            "type": "function",
            "function": {
                "name": "list_workflows",
                "description": "Get a list of all workflows for the current user.",
                "parameters": {
                    "type": "object",
                    "properties": {},
                    "additionalProperties": False
                }
            }
        },
        {
            "type": "function",
            "function": {
                "name": "dispatch_ui_actions",
                "description": "Dispatch one or multiple actions to the user's frontend. Use this to navigate pages, show toasts, or manipulate the ReactFlow canvas (add_node, connect_nodes).",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "actions": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "action_type": {
                                        "type": "string",
                                        "enum": ["navigate", "show_toast", "open_modal", "add_node", "update_node", "remove_node", "connect_nodes", "disconnect_nodes", "clear_canvas", "replace_canvas"]
                                    },
                                    "payload": {
                                        "type": "object",
                                        "description": "The payload specific to the action_type."
                                    }
                                },
                                "required": ["action_type", "payload"]
                            }
                        }
                    },
                    "required": ["actions"],
                    "additionalProperties": False
                }
            }
        }
    ]

    @classmethod
    async def execute_tool(cls, name: str, args: Dict[str, Any], context: Dict[str, Any]) -> str:
        """Execute a specific tool and return its result as a string."""
        user = context.get("user")
        
        try:
            if name == "list_workflows":
                from orchestrator.models import Workflow
                # Fetch user's workflows
                workflows = []
                async for wf in Workflow.objects.filter(user=user).all():
                    workflows.append({"id": wf.id, "name": wf.name, "status": wf.status})
                return json.dumps({"workflows": workflows})
                
            elif name == "dispatch_ui_actions":
                actions = args.get("actions", [])
                # Instead of executing it immediately, we can append it to the state's collected actions
                # So the graph can dispatch them at the end. We just return success here.
                return json.dumps({"status": "success", "actions_queued": len(actions)})
                
            else:
                return json.dumps({"error": f"Unknown tool: {name}"})
                
        except Exception as e:
            logger.exception(f"Tool execution failed: {e}")
            return json.dumps({"error": str(e)})

    @classmethod
    def get_available_tools(cls) -> List[Dict[str, Any]]:
        return cls.AVAILABLE_TOOLS
