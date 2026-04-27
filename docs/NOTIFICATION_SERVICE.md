# Notification Service & Real-time Updates

The AIAAS Notification Service provides a unified system for alerting users about system events, workflow status updates, and interactive "Human-In-The-Loop" (HITL) requests. It combines persistent database logging with real-time WebSocket broadcasting.

## 1. Architecture Overview

The system is divided into two primary layers:
1.  **Persistent Notifications (REST):** Asynchronous alerts stored in the database for historical reference (e.g., "Workflow Failed", "Image Ready").
2.  **Real-time Streams (WebSockets):** Low-latency updates for active execution monitoring and interactive decision-making.

---

## 2. Persistent Notifications

Handled by the `notifications` app, these are intended for events that the user needs to see even if they weren't online when the event occurred.

### Data Model (`Notification`)
- **Type:** Categorizes the alert (`workflow_failed`, `new_message`, `hitl_request`, `image_ready`, `system`).
- **Target:** Specific `User` via ForeignKey.
- **Payload:** Title, Message, and a `JSONField` for additional context (e.g., `execution_id`).
- **State:** `is_read` boolean for tracking user interaction.

### API Endpoints
- `GET /api/notifications/`: Retrieves the user's notification history (sorted by newest first).
- `POST /api/notifications/{id}/mark_read/`: Marks a specific notification as read.
- `POST /api/notifications/mark_all_read/`: Bulk update for all unread notifications.

### Backend Usage
To trigger a notification from any part of the backend:
```python
from notifications.utils import create_notification

create_notification(
    user=user_instance,
    type='workflow_failed',
    title='Production Workflow Failed',
    message='Workflow "Data Sync" failed at node "PostgreSQL"',
    data={'execution_id': '...'}
)
```

---

## 3. Real-time WebSocket Streams

Real-time updates are managed via **Django Channels** and are routed through `streaming/routing.py`.

### Execution Stream (`ws/execution/{execution_id}/`)
Provides granular updates for a specific workflow run.
- **execution.event:** Emitted when a node starts, finishes, or fails. Used to update the visual canvas in real-time.
- **execution.state_sync:** Sent immediately upon connection to sync the current status of all nodes.

### HITL Stream (`ws/hitl/`)
A dedicated, user-wide stream for **Human-In-The-Loop** requests.
- This allows a user to receive approval requests from *any* active workflow without being on that specific workflow's page.
- **Message Types:**
    - `new_request`: Triggered when an agent requires human intervention (approval, clarification, error recovery).
    - `response_ack`: Confirmation that the user's decision was received and processed.

---

## 4. Human-In-The-Loop (HITL) Flow

HITL is a critical feature that allows autonomous agents to "pause" and ask for permission or data.

1.  **Request Generation:** The Orchestrator or a specific Node detects a need for input and creates a `HITLRequest` record.
2.  **Notification:**
    - A `Notification` record is created (Persistent).
    - A WebSocket message is pushed to the `hitl_{user_id}` group (Real-time).
3.  **User Response:**
    - The user sees a popup or notification in the UI.
    - User sends a response via WebSocket (`type: 'hitl_response'`) or via the REST API.
4.  **Resumption:** The `ExecutionConsumer` updates the database, signals the background executor, and the workflow resumes from the paused state.

---

## 5. Frontend Integration

### Hooks
The frontend uses a custom hook `useHITLWebSocket` to maintain the user-wide notification connection.
- **Path:** `better-n8n-frontend/src/hooks/useWebSocket.ts`
- **Component:** `NotificationsTab.tsx` provides the UI for viewing and managing persistent notifications.

### Notification Types
| Type | Description | UI Action |
| :--- | :--- | :--- |
| `workflow_failed` | A workflow encountered a terminal error. | Link to Logs |
| `hitl_request` | Agent is waiting for your approval. | Open Modal / Decision UI |
| `image_ready` | DALL-E or Stable Diffusion task finished. | View Image |
| `system` | Maintenance or system-wide alerts. | Toast Notification |
