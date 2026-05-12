# Smart Orchestrator (KingOrchestrator) Vision & Discussion

## 1. The Supreme Manager
The **Smart Orchestrator**, internally known as the `KingOrchestrator`, is the central intelligence of the AIAAS platform. It serves as the bridge between high-level user intent and the deterministic execution of complex workflows.

While **Better n8n** provides the canvas and **BrowserOS** provides the interaction layer, the Smart Orchestrator is the "brain" that ensures goals are met, errors are recovered from, and the system evolves based on user feedback.

## 2. Core Capabilities
*   **Intent Translation:** Translates natural language descriptions into executable workflow structures using a specialized Knowledge Base.
*   **Goal-Oriented Supervision:** Monitors running workflows not just for technical success, but for goal achievement. It can decide to continue, retry, or pause based on the output of any node.
*   **Human-In-The-Loop (HITL):** Orchestrates interactions where AI needs human guidance, such as approvals, clarifications, or complex error recovery.
*   **Self-Healing Executions:** When a node fails, the King can analyze the error context and suggest or automatically implement recovery strategies.
*   **Context Awareness:** Maintains a deep understanding of the conversation history, active workspace state, and available "Skills" (tools).

## 3. Architectural Highlights
The `KingOrchestrator` is designed with a "Supervisor-Worker" pattern:
*   **ExecutionEngine (The Worker):** A deterministic engine that follows the workflow graph precisely.
*   **King (The Supervisor):** An LLM-powered agent that sits above the engine, observing "thoughts" and "outputs" to make runtime steering decisions.

### Key Logic in `executor/king.py`:
*   **ExecutionHandle:** A stateful object tracking the lifecycle of a single intent.
*   **Thought Generation:** The King generates internal "thoughts" before and after node executions to maintain a reasoning chain.
*   **Memory Management:** Automatic TTL-based cleanup of execution states to prevent resource leaks.

## 4. Integration with the Ecosystem
*   **With BrowserOS:** The King pushes live status updates, reasoning thoughts, and HITL requests to the BrowserOS frontend via WebSockets.
*   **With Better n8n:** The King can generate or modify `.n8n` compatible workflows dynamically.
*   **With MCP (Model Context Protocol):** Leveraging MCP to extend the King's reach into external tools and data sources.

## 5. Roadmap & Future Discussion
1.  **Distributed Orchestration:** Scaling the King to handle thousands of concurrent goal-oriented agents across a cluster.
2.  **Autonomous Refinement:** Allowing the King to "audit" its own generated workflows and optimize them for performance or cost.
3.  **Cross-Workspace Intelligence:** Enabling the King to share context and learned patterns between different user workspaces securely.
4.  **Advanced HITL Patterns:** Moving beyond simple "Approve/Reject" to collaborative multi-turn problem solving within the orchestrator view.

## 6. Open Questions
*   **Latency vs. Intelligence:** How do we balance the "thinking time" of the King with the user's need for responsive feedback?
*   **Granularity of Control:** At what point does the King's supervision become overbearing vs. helpful? 
*   **Safety Guards:** How do we implement robust hard-stops to prevent the King from executing potentially destructive multi-step plans without explicit human consent?
