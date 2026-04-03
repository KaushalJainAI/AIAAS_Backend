# Chat Agent Module

This directory contains the logic for the Standalone AI Chat Agent.

## Key Files
- `views.py`: Main entry points for chat sessions, message handling, and the agentic tool loop.
- `tools.py`: Registry of tools available to the AI (web search, code execution, etc.).
- `orchestrator.py`: Logic for model name normalization and intent analysis.
- `models.py`: Database schemas for `ChatSession`, `ChatMessage`, and `ChatAttachment`.
- `extraction.py`: Pattern matching and extraction of tool calls from LLM text.
- `schemas.py`: Pydantic models for internal data structures.

## Documentation
For a detailed deep dive into the architecture, capabilities, and API structure, please refer to the main documentation:
[**CHAT_AGENT.md**](../docs/CHAT_AGENT.md)

## Quick Start
1. The chat session is initialized via `POST /api/chat/sessions/`.
2. Messages are sent via `POST /api/chat/sessions/<id>/message/`.
3. High-confidence intents (search, research, coding) can be triggered via slash commands in the message content.
