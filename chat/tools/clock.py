"""
The wall-clock tool. Models have no clock of their own.
"""
from __future__ import annotations

import json
import logging

from typing import Dict

from .registry import tool

logger = logging.getLogger(__name__)


@tool({
        "type": "function",
        "function": {
            "name": "get_current_time",
            "description": "Get the current date and time. Use this when the user asks for the current date, time, or day of the week.",
            "parameters": {
                "type": "object",
                "properties": {},
                "additionalProperties": False
            }
        }
    }, parallel=True, effect="read")
async def get_current_time(args: Dict, context: Dict) -> str:
    import datetime
    try:
        from django.utils import timezone
        current_time = timezone.now().strftime("%A, %B %d, %Y %I:%M %p %Z")
    except Exception:
        current_time = datetime.datetime.now().strftime("%A, %B %d, %Y %I:%M %p")
    return json.dumps({"current_time": current_time})
