"""
The tool that reaches this platform's own HTTP API on the user's behalf.

Sensitive by definition: it acts as the user against real endpoints.
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
            "name": "call_internal_api",
            "description": "Call any internal REST API endpoint in the platform (e.g., /api/workflows/, /api/credentials/). Returns the JSON response from the server.",
            "parameters": {
                "type": "object",
                "properties": {
                    "method": {
                        "type": "string",
                        "enum": [
                            "GET",
                            "POST",
                            "PUT",
                            "DELETE",
                            "PATCH"
                        ],
                        "description": "The HTTP method to use."
                    },
                    "path": {
                        "type": "string",
                        "description": "The URL path (e.g., /api/workflows/, /api/credentials/1/)"
                    },
                    "data": {
                        "type": "object",
                        "description": "JSON payload for POST/PUT/PATCH requests."
                    },
                    "query_params": {
                        "type": "object",
                        "description": "Query parameters for GET requests."
                    }
                },
                "required": [
                    "method",
                    "path"
                ],
                "additionalProperties": False
            }
        }
    },
    sensitive=True,
)
async def call_internal_api(args: Dict, context: Dict) -> str:
    from asgiref.sync import sync_to_async
    from django.urls import resolve, Resolver404
    from rest_framework.test import APIRequestFactory
    from django.contrib.auth import get_user_model

    User = get_user_model()
    user_id = context.get("user_id")
    if not user_id:
        return json.dumps({"error": "No user_id found in context"})

    method = args.get("method", "GET").upper()
    path = args.get("path", "")
    data = args.get("data", {})
    query_params = args.get("query_params", {})

    if not path:
        return json.dumps({"error": "Path is required"})

    if not path.startswith("/"):
        path = "/" + path

    def _execute_request():
        try:
            user = User.objects.get(id=user_id)
        except User.DoesNotExist:
            return {"error": f"User {user_id} not found"}

        try:
            match = resolve(path)
        except Resolver404:
            return {"error": f"Endpoint not found: {path}", "status": 404}

        factory = APIRequestFactory()

        # Build full path with query params if any
        full_path = path
        if query_params:
            from urllib.parse import urlencode
            full_path = f"{path}?{urlencode(query_params)}"

        if method == "GET":
            request = factory.get(full_path)
        elif method == "POST":
            request = factory.post(full_path, data, format='json')
        elif method == "PUT":
            request = factory.put(full_path, data, format='json')
        elif method == "PATCH":
            request = factory.patch(full_path, data, format='json')
        elif method == "DELETE":
            request = factory.delete(full_path)
        else:
            return {"error": f"Unsupported method: {method}"}

        request.user = user

        try:
            # Need to manually apply DRF's authentication wrapper if force_authenticate isn't used directly on the view
            # Since we're calling the view directly, we pass the request object
            response = match.func(request, *match.args, **match.kwargs)

            # Check if it has a render method (DRF Response)
            if hasattr(response, 'render'):
                response.render()

            try:
                # Attempt to parse as JSON first
                content = json.loads(response.content.decode('utf-8'))
            except (json.JSONDecodeError, UnicodeDecodeError):
                # Fallback to string if not JSON
                content = response.content.decode('utf-8', errors='replace')

            return {
                "status_code": response.status_code,
                "data": content
            }
        except Exception as e:
            logger.exception(f"Internal API error on {method} {path}: {e}")
            return {"error": f"Internal server error: {str(e)}", "status": 500}

    # Run synchronously to avoid breaking Django ORM limits in async context
    result = await sync_to_async(_execute_request)()
    return json.dumps(result)
