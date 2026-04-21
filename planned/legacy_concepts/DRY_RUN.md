# Workflow Pipeline: Detailed Dry Run Simulation

This document provides a comprehensive, step-by-step simulation of a workflow execution. It unifies the actions of the **King Agent (Orchestrator)** and the **Execution Engine (Worker)** into a single chronological narrative.

**Objective**: To transparently inspect how the backend processes a request from API to Database to Completion, using a real-world "Commit Feedback Workflow" example with accurate JSON schema and expression syntax.

---

## 0. The Workflow Definition (Database State)

Before execution begins, the workflow exists as a row in the `Backend.orchestrator.models.Workflow` table. 

**Full JSON Representation**:
This reflects the actual schema structure stored in the database, including `nodeType`, `data.config`, and `{{ $node["Name"].json.field }}` expressions.

```json
{
  "version": "1.0.0",
  "name": "Commit Feedback Workflow",
  "createdAt": "2026-02-21T07:13:04.899Z",
  "nodes": [
    {
      "id": "node-1770295172089",
      "type": "custom",
      "nodeType": "telegram",
      "position": {
        "x": 465,
        "y": 240
      },
      "data": {
        "label": "Node",
        "icon": "✈️",
        "color": "#0088cc",
        "config": {
          "credential": "17",
          "operation": "send_message",
          "chat_id": "5386479192",
          "text": "{{ $node[\"GitHub Trigger\"].json.triggered_at }}{{ $node[\"OpenRouter\"].json.content }}{{ $node[\"OpenRouter\"].json.reasoning }}..",
          "photo_url": "eowkdkoem",
          "document_url": "",
          "parse_mode": "",
          "message_limit": 4096
        },
        "nodeType": "telegram",
        "executionStatus": "completed",
        "outputData": [
          {
            "json": {
              "message_id": 860,
              "chat_id": 5386479192,
              "date": 1771654364
            },
            "binary": null,
            "pairedItem": null
          }
        ],
        "inputData": [
          {
            "json": {
              "content": "\n\nfeat: Add array_diff function for vectorized difference calculation\n\n- Implemented vectorized array difference calculation using numpy's setdiff1d\n- Added comprehensive test suite covering edge cases (empty arrays, different dtypes)\n- Removed legacy loop-based implementation in favor of optimized solution\n- Improved code readability and maintainability through abstraction",
              "reasoning": "Okay, let's tackle this problem step by step. The user wants a professional commit message following the Conventional Commits spec. The context is that I'm a Senior Software Engineer specializing in professional commit history, and the project is on a specific branch with stats on lines added and removed.\n\nFirst, I need to analyze the code patches provided. The user mentioned using vectorization with numpy over loops, so the changes likely involve optimizing data processing. The patches include adding a function to calculate the difference between two arrays, which is vectorized. There's also a test case added to ensure the function works correctly.\n\nThe task requires the commit message to have three parts: a header under 50 characters, a body explaining the what and why, and bullet points grouping related changes. The input context uses raw messages as a guide, so I should focus on the intent behind each change.\n\nLooking at the code changes, the main additions are the `array_diff` function and its test. The function uses numpy's `setdiff1d` to find differences between two arrays, which is efficient. The test case checks edge cases like empty arrays and different data types. \n\nFor the header, I need to summarize the change concisely. Since it's a new function, \"feat\" makes sense. The header should be under 50 characters, so \"feat: Add array_diff function for vectorized difference calculation\" fits.\n\nThe body should explain the what and why. The what is adding the function, and the why is to provide a vectorized solution for array differences. Mentioning the use of numpy's `setdiff1d` shows the implementation choice. Also, note the test case to ensure correctness.\n\nGrouping related changes: the function and its test are related, so bullet points under the body. Also, mention the removal of a previous implementation if applicable, but the patches don't show that. Wait, the user's code changes only show adding the function and test. So maybe just the function and test are the changes. \n\nWait, the user's code patches include adding a function and a test. So the commit message should reflect adding the function and the test. The header is \"feat: Add array_diff function for vectorized difference calculation\". The body explains the what (adding the function) and why (vectorized approach). The bullet points would list the function and the test. \n\nAlso, check if there's any other changes. The user's context says the project is on branch [], but the branch isn't specified. But the task is to generate the commit message based on the code changes provided. \n\nSo putting it all together: header, body with what and why, bullet points for the changes. The output should only be the commit message text. Make sure to follow the Conventional Commits spec with the type (feat, fix, etc.), and the body in present tense. \n\nWait, the user's example input context uses \"feat\" as a type. The code changes are adding a function, so \"feat\" is appropriate. The body should explain the changes and their purpose. The bullet points should group related changes, which here are the function and the test. \n\nI think that's all. Now format the commit message accordingly.\n",
              "model": "arcee-ai/trinity-mini:free",
              "requested_model": "arcee-ai/trinity-mini:free",
              "used_fallback": false,
              "generation_id": "gen-1771654358-3I60uDP3LFIeZIAX72nP",
              "finish_reason": "stop",
              "usage": {
                "prompt_tokens": 197,
                "completion_tokens": 713,
                "total_tokens": 910
              },
              "input": {
                "project_context": {
                  "repository": "AIAAS_Backend",
                  "branch": null,
                  "head_sha": null,
                  "sender": null
                },
                "change_summary": {
                  "commit_count": 0,
                  "total_additions": 0,
                  "total_deletions": 0,
                  "messages": []
                },
                "code_changes": [],
                "raw_payload": {},
                "triggered_at": "2026-02-21T11:42:39.257862",
                "repository": "AIAAS_Backend",
                "event": "unknown",
                "action": "",
                "sender": {},
                "ref": "",
                "payload": {}
              }
            },
            "binary": null,
            "pairedItem": null
          }
        ]
      }
    },
    {
      "id": "node-1770827849141",
      "type": "trigger",
      "nodeType": "github_trigger",
      "position": {
        "x": -75,
        "y": 240
      },
      "data": {
        "label": "GitHub Trigger",
        "icon": "🐙",
        "color": "#22c55e",
        "config": {
          "credential": "16",
          "repository": "AIAAS_Backend",
          "events": [
            "push",
            "pull_request"
          ],
          "branch_filter": "main",
          "include_raw": false
        },
        "nodeType": "github_trigger",
        "executionStatus": "completed",
        "errorMessage": "",
        "outputData": [
          {
            "json": {
              "project_context": {
                "repository": "AIAAS_Backend",
                "branch": null,
                "head_sha": null,
                "sender": null
              },
              "change_summary": {
                "commit_count": 0,
                "total_additions": 0,
                "total_deletions": 0,
                "messages": []
              },
              "code_changes": [],
              "raw_payload": {},
              "triggered_at": "2026-02-21T11:42:39.257862",
              "repository": "AIAAS_Backend",
              "event": "unknown",
              "action": "",
              "sender": {},
              "ref": "",
              "payload": {}
            },
            "binary": null,
            "pairedItem": null
          }
        ]
      }
    },
    {
      "id": "node-1771514556675",
      "type": "custom",
      "nodeType": "openrouter",
      "position": {
        "x": 195,
        "y": 240
      },
      "data": {
        "label": "OpenRouter",
        "icon": "🌐",
        "color": "#6366f1",
        "config": {
          "credential": "21",
          "model": "arcee-ai/trinity-mini:free",
          "prompt": "# Role You are a Senior Software Engineer specializing in professional commit history. # Context Project: {{ event.project_context.repository }} on branch [] Author: Stats: + / - lines across commits. # Task Analyze the code patches below and generate a professional commit message following the **Conventional Commits** spec (feat, fix, refactor, etc.). # Code Changes (Patches) # Specific Instructions 1. First line: Header (max 50 chars). 2. Body: Explain the 'what' and 'why' behind the changes. 3. Logical Grouping: Group related changes together using bullet points. 4. Input Context: Use these raw messages as a guide for intent: # Output Return ONLY the commit message text.",
          "system_message": "You are a helpful assistant.",
          "skills": [
            "9"
          ],
          "temperature": 0.3,
          "max_tokens": 2048,
          "top_p": 1,
          "response_format": "text"
        },
        "nodeType": "openrouter",
        "executionStatus": "completed",
        "inputData": [
          {
            "json": {
              "project_context": {
                "repository": "AIAAS_Backend",
                "branch": null,
                "head_sha": null,
                "sender": null
              },
              "change_summary": {
                "commit_count": 0,
                "total_additions": 0,
                "total_deletions": 0,
                "messages": []
              },
              "code_changes": [],
              "raw_payload": {},
              "triggered_at": "2026-02-21T11:42:39.257862",
              "repository": "AIAAS_Backend",
              "event": "unknown",
              "action": "",
              "sender": {},
              "ref": "",
              "payload": {}
            },
            "binary": null,
            "pairedItem": null
          }
        ],
        "outputData": [
          {
            "json": {
              "content": "\n\nfeat: Add array_diff function for vectorized difference calculation\n\n- Implemented vectorized array difference calculation using numpy's setdiff1d\n- Added comprehensive test suite covering edge cases (empty arrays, different dtypes)\n- Removed legacy loop-based implementation in favor of optimized solution\n- Improved code readability and maintainability through abstraction",
              "reasoning": "Okay, let's tackle this problem step by step. The user wants a professional commit message following the Conventional Commits spec. The context is that I'm a Senior Software Engineer specializing in professional commit history, and the project is on a specific branch with stats on lines added and removed.\n\nFirst, I need to analyze the code patches provided. The user mentioned using vectorization with numpy over loops, so the changes likely involve optimizing data processing. The patches include adding a function to calculate the difference between two arrays, which is vectorized. There's also a test case added to ensure the function works correctly.\n\nThe task requires the commit message to have three parts: a header under 50 characters, a body explaining the what and why, and bullet points grouping related changes. The input context uses raw messages as a guide, so I should focus on the intent behind each change.\n\nLooking at the code changes, the main additions are the `array_diff` function and its test. The function uses numpy's `setdiff1d` to find differences between two arrays, which is efficient. The test case checks edge cases like empty arrays and different data types. \n\nFor the header, I need to summarize the change concisely. Since it's a new function, \"feat\" makes sense. The header should be under 50 characters, so \"feat: Add array_diff function for vectorized difference calculation\" fits.\n\nThe body should explain the what and why. The what is adding the function, and the why is to provide a vectorized solution for array differences. Mentioning the use of numpy's `setdiff1d` shows the implementation choice. Also, note the test case to ensure correctness.\n\nGrouping related changes: the function and its test are related, so bullet points under the body. Also, mention the removal of a previous implementation if applicable, but the patches don't show that. Wait, the user's code changes only show adding the function and test. So maybe just the function and test are the changes. \n\nWait, the user's code patches include adding a function and a test. So the commit message should reflect adding the function and the test. The header is \"feat: Add array_diff function for vectorized difference calculation\". The body explains the what (adding the function) and why (vectorized approach). The bullet points would list the function and the test. \n\nAlso, check if there's any other changes. The user's context says the project is on branch [], but the branch isn't specified. But the task is to generate the commit message based on the code changes provided. \n\nSo putting it all together: header, body with what and why, bullet points for the changes. The output should only be the commit message text. Make sure to follow the Conventional Commits spec with the type (feat, fix, etc.), and the body in present tense. \n\nWait, the user's example input context uses \"feat\" as a type. The code changes are adding a function, so \"feat\" is appropriate. The body should explain the changes and their purpose. The bullet points should group related changes, which here are the function and the test. \n\nI think that's all. Now format the commit message accordingly.\n",
              "model": "arcee-ai/trinity-mini:free",
              "requested_model": "arcee-ai/trinity-mini:free",
              "used_fallback": false,
              "generation_id": "gen-1771654358-3I60uDP3LFIeZIAX72nP",
              "finish_reason": "stop",
              "usage": {
                "prompt_tokens": 197,
                "completion_tokens": 713,
                "total_tokens": 910
              },
              "input": {
                "project_context": {
                  "repository": "AIAAS_Backend",
                  "branch": null,
                  "head_sha": null,
                  "sender": null
                },
                "change_summary": {
                  "commit_count": 0,
                  "total_additions": 0,
                  "total_deletions": 0,
                  "messages": []
                },
                "code_changes": [],
                "raw_payload": {},
                "triggered_at": "2026-02-21T11:42:39.257862",
                "repository": "AIAAS_Backend",
                "event": "unknown",
                "action": "",
                "sender": {},
                "ref": "",
                "payload": {}
              }
            },
            "binary": null,
            "pairedItem": null
          }
        ]
      }
    }
  ],
  "edges": [
    {
      "id": "e-node-1770827849141-output-0-node-1771514556675",
      "source": "node-1770827849141",
      "target": "node-1771514556675",
      "sourceHandle": "output-0"
    },
    {
      "id": "reactflow__edge-node-1771514556675output-0-node-1770295172089input-0",
      "source": "node-1771514556675",
      "target": "node-1770295172089",
      "sourceHandle": "output-0",
      "targetHandle": "input-0"
    }
  ]
}
```

---

## 1. Phase 1: API Request & Orchestrator Boot

**Scenario**: A GitHub webhook pushes a payload, triggering the workflow via the backend webhook receiver.

1.  **Auth & Mapping**: Webhook receiver identifies the target workflow through headers.
2.  **Global Input Mapping**: The GitHub payload is stored in the engine's initial state as `_input_global`. (Available via the `{{ event.xxx }}` expression).
3.  **Handoff**: Calls `KingOrchestrator.start()`.
4.  **Database**: `ExecutionLog` is created in `running` state.

---

## 2. Phase 2: Compilation (`compiler.py`)

The Engine takes over. The raw JSON is compiled into an executable LangGraph StateGraph.

1.  **Fail-Fast Checks**: Validates DAG cycles (`github_trigger` -> `openrouter` -> `telegram`).
2.  **Credential Lookup**: Validates credentials `16`, `21`, and `17` belong to the user.
3.  **Graph Construction**: It wraps every node's execution logic in a wrapper function, injecting `KingOrchestrator` supervision hooks (`before_node` and `after_node`).

---

## 3. Phase 3: The Graph Loop (Step-by-Step)

The engine invokes the graph. We follow the execution path.

### Node 1: GitHub Trigger (`node-1770827849141`)
1.  **Execution**: Extracts global payload.
2.  **Result**: Returns the parsed github event payload indicating a commit.
3.  **Global State Update**: `state['node_outputs']["node-1770827849141"]` and internal label mapping `state['node_outputs']["GitHub Trigger"]` are set.
    > 📚 See [STATE_MANAGEMENT.md](./STATE_MANAGEMENT.md) to understand how `node_outputs` are recorded in the LangGraph `WorkflowState`.

### Node 2: OpenRouter (`node-1771514556675`)
1.  **Expression Resolution**: Uses `{{ event.project_context.repository }}` against `_input_global` to inject "AIAAS_Backend" into the LLM prompt.
2.  **Execution**: Invokes OpenRouter API using credential `21`.
3.  **Result**: Returns `{ "content": "feat: Add array_diff...", "reasoning": "..." }`.

### Node 3: Telegram (`node-1770295172089`)
1.  **Expression Resolution**: 
    - `{{ $node["GitHub Trigger"].json.triggered_at }}` pulls the timestamp from the first node's output.
    - `{{ $node["OpenRouter"].json.content }}` pulls the generated commit message from the previous node.
2.  **Execution**: Sends the compiled text message to Telegram chat `5386479192`.

---

## 4. Phase 4: King Agent Supervision Hooks

Between nodes, the King Orchestrator intercepts execution.

### 4.1 Check Before Execution (`before_node`)
Right before `OpenRouter` runs, the King's hook is called (if `supervision_level` permits).

1.  **Goal-Oriented Reasoning**: The Orchestrator calls its internal LLM (`_generate_thought`) to analyze the current node against the `execution_goal`. It generates an insight stream.
2.  **Dynamic Conditions**: It checks `goal_conditions` to decide if execution should continue.
3.  **Pause Checks**: It checks `_pause_events[exec_id]`.
4.  **Cancel Checks**: If `state == CANCELLED`, it throws an `AbortDecision` to kill the graph immediately.

### 4.2 Check After Execution (`after_node`)
Right after `OpenRouter` finishes.

1.  **Loop Protection**: King checks if node output targets a loop. If `loop_counters > 1000`, the King panics and throws an `AbortDecision`.

---

## 5. Phase 5: Completion & Garbage Collection

### 5.1 Final DB Update
The graph completes. The Engine returns `COMPLETED`. The King updates the master record in database.

### 5.2 Memory Eviction
Background `_cleanup_loop` garbage collector eventually purges the UUID from the `_executions` dict and checks the database for stalled workers (Zombicide).
