# Platform Copilot (formerly Canvas Agent)

## Overview
The Platform Copilot is an AI-powered assistant designed to allow users to control the entire application via natural language commands. Originally designed solely as the "Canvas Agent" to manipulate the ReactFlow workflow editor, it has been evolved into a general platform controller. It translates user instructions into a structured array of JSON actions (`dispatch_ui_actions`) and can read/mutate platform state via a generic backend API caller (`call_internal_api`).

## Architecture & Integration
The Copilot bridges the gap between natural language interfaces, backend REST APIs, and structured node-based workflow generation. 

It is integrated natively into the global frontend `ChatPanel`. When a user prefixes their message with `/copilot`, the chat panel routes the request to this specific agent rather than the standard chat orchestrator.

### Flow of Execution
1. **Frontend Request:** The frontend captures a user's natural language command (e.g., "/copilot Go to my credentials and show a success toast").
2. **Context Construction:** The backend receives the command along with context:
   - `current_url`: The route the user is currently viewing.
   - `canvas_state`: The current state of the ReactFlow canvas (nodes and edges), if applicable.
   - The schemas for all available workflow node types.
3. **LangGraph Agentic Loop:** The backend uses a `StateGraph` (from LangGraph) to process the request:
   - The Agent node evaluates the instruction and decides whether to call a tool (e.g., `list_workflows`, `call_internal_api`, or `dispatch_ui_actions`).
   - The Tool node executes the backend logic securely on behalf of the user.
   - The loop iterates until the LLM has satisfied the user's intent.
4. **Action Dispatch:** Any requested UI actions (e.g., navigating, modifying the canvas) are batched and pushed back to the frontend in real-time via a Django Channels WebSocket group (`canvas_agent_{user_id}`).

## Supported UI Actions (`dispatch_ui_actions`)
The Copilot can control the frontend by dispatching these actions over the WebSocket:

* **`navigate`**: Changes the frontend route (e.g., `/workflows`, `/credentials`).
* **`show_toast`**: Displays a success/error toast notification to the user.
* **`open_modal`**: Instructs the frontend to open a specific dialog/modal.
* **`add_node`**: Creates a new node on the canvas, automatically predicting (x, y) coordinates to avoid overlap.
* **`update_node`**: Modifies the configuration of an existing workflow node.
* **`remove_node`**: Deletes a node.
* **`connect_nodes`**: Creates an edge between a `source_id` and a `target_id`.
* **`disconnect_nodes`**: Removes an edge.
* **`clear_canvas`**: Clears the entire canvas.
* **`replace_canvas`**: Overwrites the entire canvas state with a new set of nodes and edges.

## Backend Tools
The Copilot (and the main Chat Agent) has access to powerful backend tools defined in `tools.py`:
* **`call_internal_api`**: A generic tool that simulates authenticated Django API requests. It can perform GET, POST, PUT, PATCH, and DELETE operations against any internal platform endpoint (like `/api/workflows/` or `/api/credentials/`). This runs securely using `APIRequestFactory` bound to the user's ID.
* **`list_workflows`**: A specific tool to retrieve the user's workflows.
* **`dispatch_ui_actions`**: The tool used to queue UI modifications for the WebSocket broadcast.

## WebSocket Communication
Real-time operations are handled via Django Channels.

* **Consumer:** `CanvasAgentConsumer`
* **Group Name:** `canvas_agent_{user_id}`
* **Handled Events:**
  - `canvas_state` (Receive): The frontend periodically sends the current canvas state, cached for context.
  - `dispatch_actions` (Send): Pushes batched UI or node manipulation actions to the client.

## Security and Credentials
The Copilot respects the platform's multi-tenant architecture. 
1. It uses `_get_user_llm_credentials` to dynamically fetch the user's verified API key from the `credentials` app before making LLM calls.
2. Tools like `call_internal_api` explicitly bind the Django `Request` object to the authenticated `user_id`, ensuring the AI can only access data the user owns.