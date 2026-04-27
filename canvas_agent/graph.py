import json
import logging
from typing import TypedDict, Annotated, List, Dict, Any
from langgraph.graph import StateGraph, END
from langchain_core.messages import BaseMessage, HumanMessage, AIMessage, ToolMessage
from langgraph.graph.message import add_messages
import litellm

from .tools import ToolExecutor

logger = logging.getLogger(__name__)

class CopilotState(TypedDict):
    messages: Annotated[list[BaseMessage], add_messages]
    user: Any
    creds: Dict[str, str]
    model: str
    system_prompt: str
    ui_actions: List[Dict[str, Any]]
    iterations: int


def _convert_openai_tc_to_langchain(raw_tool_calls: list) -> list[dict]:
    converted = []
    for i, tc in enumerate(raw_tool_calls):
        if tc.get("type") != "function":
            continue
        func = tc.get("function", {})
        name = func.get("name", "")
        if not name:
            continue
        args_str = func.get("arguments", "{}")
        try:
            args = json.loads(args_str)
        except:
            args = {"query": args_str}
        converted.append({
            "name": name,
            "args": args,
            "id": tc.get("id", f"call_{i}")
        })
    return converted

async def agent_node(state: CopilotState) -> dict:
    """Invoke the LLM with the current message history and tools."""
    messages = state["messages"]
    
    # Format messages for LiteLLM
    formatted_messages = [{"role": "system", "content": state["system_prompt"]}]
    for msg in messages:
        if isinstance(msg, HumanMessage):
            formatted_messages.append({"role": "user", "content": msg.content})
        elif isinstance(msg, AIMessage):
            m = {"role": "assistant", "content": msg.content or ""}
            if msg.tool_calls:
                # Convert LangChain tool_calls to LiteLLM tool_calls format
                m["tool_calls"] = [
                    {
                        "id": tc.get("id"),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("args", {}))
                        }
                    } for tc in msg.tool_calls
                ]
            formatted_messages.append(m)
        elif isinstance(msg, ToolMessage):
            formatted_messages.append({
                "role": "tool",
                "tool_call_id": msg.tool_call_id,
                "name": msg.name,
                "content": str(msg.content)
            })

    tools = ToolExecutor.get_available_tools()
    
    kwargs = {}
    if state["creds"]:
        kwargs.update(state["creds"])

    try:
        response = await litellm.acompletion(
            model=state["model"],
            messages=formatted_messages,
            tools=tools,
            tool_choice="auto",
            **kwargs
        )
        
        msg = response.choices[0].message
        content = msg.content or ""
        raw_tool_calls = msg.tool_calls or []
        
        if not raw_tool_calls and not content:
            content = "I've processed your request."
            
        lc_tool_calls = _convert_openai_tc_to_langchain(raw_tool_calls) if hasattr(msg, 'tool_calls') and msg.tool_calls else []
        
        ai_msg = AIMessage(content=content, tool_calls=lc_tool_calls)
        
        return {
            "messages": [ai_msg],
            "iterations": state.get("iterations", 0) + 1
        }
        
    except Exception as e:
        logger.exception(f"Error calling LLM: {e}")
        return {
            "messages": [AIMessage(content=f"Error executing command: {str(e)}")]
        }

async def tools_node(state: CopilotState) -> dict:
    """Execute tools requested by the LLM."""
    last_msg = state["messages"][-1]
    if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
        return {"messages": []}
        
    tool_messages = []
    collected_ui_actions = []
    
    for tc in last_msg.tool_calls:
        name = tc["name"]
        args = tc.get("args", {})
        call_id = tc.get("id", f"call_{name}")
        
        # Special handling to intercept UI actions before returning them to LLM
        if name == "dispatch_ui_actions":
            actions = args.get("actions", [])
            collected_ui_actions.extend(actions)
            result = json.dumps({"status": "success", "actions_dispatched": len(actions)})
        else:
            context = {"user": state["user"]}
            result = await ToolExecutor.execute_tool(name, args, context)
            
        tool_messages.append(ToolMessage(
            content=result,
            tool_call_id=call_id,
            name=name
        ))
        
    return {
        "messages": tool_messages,
        "ui_actions": collected_ui_actions
    }

def should_continue(state: CopilotState) -> str:
    """Route to tools or end depending on if the LLM called a tool."""
    last_msg = state["messages"][-1]
    if isinstance(last_msg, AIMessage) and last_msg.tool_calls:
        # Prevent infinite loops
        if state.get("iterations", 0) > 10:
            return END
        return "tools"
    return END

# Build Graph
builder = StateGraph(CopilotState)
builder.add_node("agent", agent_node)
builder.add_node("tools", tools_node)
builder.set_entry_point("agent")
builder.add_conditional_edges("agent", should_continue, {"tools": "tools", END: END})
builder.add_edge("tools", "agent")
copilot_graph = builder.compile()
