# Holistic AI Orchestrator Design ("King Agent" Architecture)

## Vision
The Orchestrator is the **King Agent**: a supreme, LLM-based intelligence layer that sits *above* the deterministic workflow engine. It translates the user's Natural Language intent into precise technical actions, managing the entire lifecycle of creation, execution, and modification.

## The Hierarchy

### Layer 1: The King Agent (Orchestrator)
*   **Role**: The "Manager". Speaks English, thinks strategically.
*   **Interface**: Chat / Voice.
*   **Capabilities**:
    *   **Translation**: Converts "My leads are stuck" $\to$ `AnalysisWorkflow(target='leads_pipeline')`.
    *   **Tool Use**: It has "God Mode" access to all API endpoints:
        *   `create_workflow()`
        *   `modify_node(id, new_prompt)`
        *   `inject_data(context_id, data)`
        *   `ask_human(question)`
    *   **Prompt Injection**: It can dynamically re-write prompts within nodes based on what it learns from the user.

### Layer 2: The Execution Engine (Deterministic)
*   **Role**: The "Worker". Executes logic flawlessly.
*   **Interface**: JSON / StateGraph.
*   **Capabilities**:
    *   Runs the static/dynamic workflows defined by the King Agent.
    *   Guarantees reliability and observability (Glass Box).

## Core Capabilities of the King Agent

### 1. Natural Language Control
The user never needs to know what a "Node" or "Edge" is.
*   **User**: "This generic email is too formal."
*   **King Agent**: Understands this as a feedback signal. It:
    1.  Locates the `EmailGenerationNode` in the active workflow.
    2.  Calls `modify_node` to update the system prompt: *"Make the tone casual and friendly."*
    3.  Re-runs the specific node to show the difference.

### 2. Dynamic Data Injection
The King Agent can inject context that isn't in the database.
*   **User**: "Here is the new pricing PDF, use this for the next run."
*   **King Agent**:
    1.  Parses the PDF (using a tool).
    2.  Injects the extracted text into the `WorkflowContext` as a variable `pricing_policy`.
    3.  Directs the `QuoteGeneratorNode` to use this new variable.

### 3. Full Autonomy (Lifecycle Management)
The King Agent doesn't just "run" workflows; it tends to them.
*   **Monitoring**: "I noticed the 'Lead Scraper' failed 3 times. Shall I switch the provider to Google Search?"
*   **Notification**: "I finished the report. Sending it to your Slack now."

## System Architecture

```mermaid
graph TD
    User[User] -->|Natural Language| King[King Agent (LLM)]
    
    subgraph "King's Toolkit"
        King -->|Draft/Edit| Editor[Workflow Builder]
        King -->|Inject Context| Context[Data Manager]
        King -->|Ask/Notify| Comms[Communication Hub]
    end
    
    Editor -->|Defines| Engine[Deterministic Engine]
    Context -->|Feeds| Engine
    
    Engine -->|Execution Results| King
    King -->|Summarized Answer| User
```

## Summary
The **King Agent** wraps the technical complexity of the workflow engine in a conversational interface. It empowers non-technical users to build, debug, and steer complex automations simply by talking.
