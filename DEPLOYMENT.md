# Workflow Deployment & Triggers

This document explains how "Deploying" a workflow works in AIAAS, including the underlying infrastructure for Webhooks and Scheduled tasks.

## The Deployment Lifecycle

In AIAAS, "Deploying" is a status transition from **Draft** to **Active**. It transforms a static workflow design into a live, listening execution.

1.  **Preparation**: The user builds a workflow and performs at least one successful manual test run.
2.  **Activation**: When the user clicks "Deploy", the backend performs:
    *   **Static Validation**: Checks for cycles, orphan nodes, and valid configurations.
    *   **Functional Proof of Success**: Verifies that the current configuration has been successfully tested.
    *   **Trigger Registration**: Scans the workflow for `trigger` nodes and registers them in the **Trigger Manager**.

---

## Trigger Infrastructure (Redis-Backed)

To ensure scalability across multiple server processes and workers, AIAAS uses **Redis** as a shared, fast-access registry for all active triggers.

### 1. Webhook Receivers
When a workflow with a `Webhook Trigger` is activated:
*   A key is added to Redis: `webhook:{user_id}/{unique_path}`.
*   The system exposes a public endpoint: `POST /api/webhooks/{user_id}/{unique_path}`.
*   **Incoming Requests**: When the endpoint is hit, it looks up the workflow ID in Redis (~0.5ms) and queues an execution on Celery with the request headers/body/query as input data.

### 2. Scheduled Triggers
When a workflow with a `Schedule Trigger` (Cron or Interval) is activated:
*   The system creates/updates a **Periodic Task** using `django-celery-beat`.
*   **Execution**: Celery Beat monitors these tasks and sends an execution request to the worker pool at the precise scheduled time.

---

## Key Components

| Component | Responsibility |
| :--- | :--- |
| `TriggerManager` | Singleton service managing Redis registrations and Celery Beat tasks. |
| `webhook_views.py` | Public endpoint for external services (Stripe, GitHub, etc.) to trigger workflows. |
| `orchestrator/apps.py` | Re-registers all `active` triggers on server startup to ensure sync. |
| `urls.py` | Routes incoming webhooks to the receiver. |

---

## Scalability Notes

*   **In-Memory vs Redis**: Unlike local `dict` registries, the Redis backend allows multiple Django processes (Gunicorn workers) and multiple physical servers to share the same trigger state.
*   **Performance**: Webhook lookups are O(1) in Redis, adding sub-millisecond overhead to incoming requests.
*   **Resilience**: Trigger data survives server restarts. The startup recovery logic ensures that even if Redis is cleared, the system re-populates it from the primary SQL database.
