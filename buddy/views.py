import re
from typing import Optional

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from browserOS.models import OSAppWindow, OSNotification, OSWorkspace
from mcp_integration.client import MCPClientManager, get_all_tools_from_all_servers


APP_ALIASES = {
    "files": "explorer",
    "file explorer": "explorer",
    "explorer": "explorer",
    "settings": "settings",
    "clock": "clock",
    "terminal": "terminal",
    "askbuddy": "chatbot",
    "buddy": "chatbot",
    "chatbot": "chatbot",
    "chat": "chatbot",
    "pixelcanvas": "image-editor",
    "image editor": "image-editor",
    "scenecraft": "video-editor",
    "video editor": "video-editor",
    "slidemaster": "presentation-editor",
    "presentation editor": "presentation-editor",
    "presentation": "presentation-editor",
    "docwriter": "word-editor",
    "word editor": "word-editor",
    "document editor": "word-editor",
    "flowforge": "diagram-editor",
    "diagram editor": "diagram-editor",
    "datalab": "analyst",
    "analyst": "analyst",
    "vectorstudio": "svg-maker",
    "svg maker": "svg-maker",
    "gridcalc": "sheets-editor",
    "sheets": "sheets-editor",
    "cloudvault": "drive",
    "drive": "drive",
    "webweaver": "frontend-expert",
    "frontend expert": "frontend-expert",
    "calculator": "calculator",
    "calc": "calculator",
    "game": "game",
    "simulator": "simulator",
    "clipboard": "clipboard",
    "screen capture": "screenshot",
    "screenshot": "screenshot",
}

APP_TITLES = {
    "explorer": "Files",
    "settings": "Settings",
    "clock": "Clock",
    "terminal": "Terminal",
    "chatbot": "AskBuddy",
    "image-editor": "PixelCanvas",
    "video-editor": "SceneCraft",
    "presentation-editor": "SlideMaster",
    "word-editor": "DocWriter",
    "diagram-editor": "FlowForge",
    "analyst": "DataLab",
    "svg-maker": "VectorStudio",
    "sheets-editor": "GridCalc",
    "drive": "CloudVault",
    "frontend-expert": "WebWeaver",
    "calculator": "CalcPro",
    "game": "SpaceQuest",
    "simulator": "SimWorld",
    "clipboard": "Clipboard History",
    "screenshot": "Screen Capture",
}

BROWSER_TOOL_ALIASES = {
    "browser_navigate": "puppeteer_navigate",
    "browser_click": "puppeteer_click",
    "browser_fill": "puppeteer_fill",
    "browser_evaluate": "puppeteer_evaluate",
    "browser_screenshot": "puppeteer_screenshot",
}


def _normalize_command(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


def _extract_quoted_value(text: str) -> Optional[str]:
    match = re.search(r'"([^"]+)"|\'([^\']+)\'', text)
    if not match:
        return None
    return match.group(1) or match.group(2)


def _resolve_app_id(command: str) -> Optional[str]:
    normalized = _normalize_command(command)
    for alias in sorted(APP_ALIASES, key=len, reverse=True):
        if re.search(rf"\b{re.escape(alias)}\b", normalized):
            return APP_ALIASES[alias]
    return None


def _get_workspace(user):
    workspace, _ = OSWorkspace.objects.get_or_create(user=user)
    return workspace


def _top_z_index(workspace: OSWorkspace) -> int:
    top = workspace.windows.order_by("-z_index").values_list("z_index", flat=True).first()
    return top or 0


def _get_window_for_app(workspace: OSWorkspace, app_id: str) -> Optional[OSAppWindow]:
    return workspace.windows.filter(app_id=app_id).order_by("-updated_at", "-z_index").first()


def _serialize_window(window: OSAppWindow) -> dict:
    return {
        "id": window.id,
        "app_id": window.app_id,
        "title": window.title,
        "is_minimized": window.is_minimized,
        "is_pinned": window.is_pinned,
        "position_x": window.position_x,
        "position_y": window.position_y,
        "width": window.width,
        "height": window.height,
        "z_index": window.z_index,
        "state_data": window.state_data,
    }


def _send_action_event(user_id: int, action: str, parameters: dict) -> None:
    channel_layer = get_channel_layer()
    if not channel_layer:
        return
    async_to_sync(channel_layer.group_send)(
        f"buddy_{user_id}",
        {
            "type": "trigger_action",
            "action": action,
            "parameters": parameters,
        },
    )


def _open_app(user, app_id: str, command_text: str) -> dict:
    workspace = _get_workspace(user)
    window = _get_window_for_app(workspace, app_id)
    created = False
    if window is None:
        created = True
        window = OSAppWindow.objects.create(
            workspace=workspace,
            app_id=app_id,
            title=APP_TITLES.get(app_id, app_id.replace("-", " ").title()),
            z_index=_top_z_index(workspace) + 1,
        )
    else:
        window.is_minimized = False
        window.z_index = _top_z_index(workspace) + 1
        window.save(update_fields=["is_minimized", "z_index", "updated_at"])

    payload = {
        "app_id": app_id,
        "window": _serialize_window(window),
        "created": created,
        "command_text": command_text,
    }
    _send_action_event(user.id, "os_open_app", payload)
    return payload


def _update_window_state(user, app_id: str, command_text: str, *, action: str, **changes) -> Optional[dict]:
    workspace = _get_workspace(user)
    window = _get_window_for_app(workspace, app_id)
    if window is None:
        return None

    for field, value in changes.items():
        setattr(window, field, value)
    if action == "os_focus_app":
        window.z_index = _top_z_index(workspace) + 1
        window.is_minimized = False
    update_fields = list(changes.keys()) + ["updated_at"]
    if action == "os_focus_app":
        update_fields.extend(["z_index", "is_minimized"])
    window.save(update_fields=list(dict.fromkeys(update_fields)))

    payload = {
        "app_id": app_id,
        "window": _serialize_window(window),
        "command_text": command_text,
    }
    _send_action_event(user.id, action, payload)
    return payload


def _close_app(user, app_id: str, command_text: str) -> Optional[dict]:
    workspace = _get_workspace(user)
    window = _get_window_for_app(workspace, app_id)
    if window is None:
        return None

    payload = {
        "app_id": app_id,
        "window_id": window.id,
        "title": window.title,
        "command_text": command_text,
    }
    window.delete()
    _send_action_event(user.id, "os_close_app", payload)
    return payload


def _create_notification(user, message: str, command_text: str, level: str = "info") -> dict:
    notification = OSNotification.objects.create(
        user=user,
        title="Buddy Notification",
        message=message,
        type=level,
    )
    payload = {
        "notification": {
            "id": notification.id,
            "title": notification.title,
            "message": notification.message,
            "type": notification.type,
            "is_read": notification.is_read,
        },
        "command_text": command_text,
    }
    _send_action_event(user.id, "os_notify", payload)
    return payload


def _set_wallpaper(user, wallpaper: str, command_text: str) -> dict:
    workspace = _get_workspace(user)
    theme_preferences = dict(workspace.theme_preferences or {})
    theme_preferences["wallpaper"] = wallpaper
    workspace.theme_preferences = theme_preferences
    workspace.save(update_fields=["theme_preferences", "updated_at"])
    payload = {
        "wallpaper": wallpaper,
        "workspace_id": workspace.id,
        "command_text": command_text,
    }
    _send_action_event(user.id, "os_set_wallpaper", payload)
    return payload


def _extract_url(command: str) -> Optional[str]:
    match = re.search(r"(https?://[^\s]+)", command, re.IGNORECASE)
    if match:
        return match.group(1)
    match = re.search(r"\b(?:go to|navigate to|open)\s+([a-zA-Z0-9.-]+\.[a-zA-Z]{2,}(?:/[^\s]*)?)", command, re.IGNORECASE)
    if match:
        url = match.group(1)
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        return url
    return None


def _find_browser_tool(user, requested_tool: str) -> Optional[dict]:
    tools = async_to_sync(get_all_tools_from_all_servers)(user)
    exact_matches = [tool for tool in tools if tool.get("name") == requested_tool]
    if exact_matches:
        preferred = sorted(
            exact_matches,
            key=lambda item: 0 if "puppeteer" in (item.get("server_name") or "").lower() else 1,
        )[0]
        return preferred
    return None


def _execute_browser_tool(user, action: str, parameters: dict) -> dict:
    requested_tool = BROWSER_TOOL_ALIASES[action]
    matched_tool = _find_browser_tool(user, requested_tool)
    if not matched_tool:
        return {
            "status": "pending_frontend",
            "message": (
                "No enabled Puppeteer MCP tool is available for this user yet. "
                "The action was still emitted to the BrowserOS frontend."
            ),
        }

    manager = MCPClientManager(matched_tool["server_id"], user=user)
    result = async_to_sync(manager.call_tool)(matched_tool["name"], parameters)
    return {
        "status": "executed",
        "server_id": matched_tool["server_id"],
        "server_name": matched_tool.get("server_name"),
        "tool_name": matched_tool["name"],
        "result": result,
    }


def _parse_text_command(command: str) -> Optional[dict]:
    normalized = _normalize_command(command)
    if not normalized:
        return None

    url = _extract_url(command)
    if url and re.search(r"\b(go to|navigate|browse|visit|open)\b", normalized):
        return {"action": "browser_navigate", "parameters": {"url": url}}

    if normalized.startswith("click "):
        selector = command.split(" ", 1)[1].strip()
        return {"action": "browser_click", "parameters": {"selector": selector}}

    fill_match = re.search(
        r'\b(?:type|fill|enter)\s+["\'](?P<value>.+?)["\']\s+(?:into|in|on)\s+(?P<selector>.+)$',
        command,
        re.IGNORECASE,
    )
    if fill_match:
        return {
            "action": "browser_fill",
            "parameters": {
                "value": fill_match.group("value").strip(),
                "selector": fill_match.group("selector").strip(),
            },
        }

    evaluate_match = re.search(r"\b(?:run js|run javascript|evaluate|execute javascript)\s+(.+)$", command, re.IGNORECASE)
    if evaluate_match:
        return {"action": "browser_evaluate", "parameters": {"script": evaluate_match.group(1).strip()}}

    if re.search(r"\b(?:take|capture)\s+(?:a\s+)?screenshot\b", normalized):
        screenshot_name = _extract_quoted_value(command) or "buddy_capture"
        return {"action": "browser_screenshot", "parameters": {"name": screenshot_name}}

    if re.search(r"\b(?:show|send|create)\s+(?:me\s+)?(?:a\s+)?notification\b", normalized):
        quoted = _extract_quoted_value(command)
        message = quoted or re.sub(r"^.*?\bnotification\b", "", command, flags=re.IGNORECASE).strip(" :,-")
        if message:
            return {"action": "os_notify", "parameters": {"message": message}}

    wallpaper_match = re.search(r"\b(?:set|change)\s+(?:the\s+)?wallpaper\s+(?:to\s+)?(.+)$", command, re.IGNORECASE)
    if wallpaper_match:
        wallpaper = wallpaper_match.group(1).strip().strip('"').strip("'")
        if wallpaper:
            return {"action": "os_set_wallpaper", "parameters": {"wallpaper": wallpaper}}

    app_id = _resolve_app_id(command)
    if app_id:
        if re.search(r"\b(?:open|launch|start|show)\b", normalized):
            return {"action": "os_open_app", "parameters": {"app_id": app_id}}
        if re.search(r"\b(?:focus|switch to|bring up)\b", normalized):
            return {"action": "os_focus_app", "parameters": {"app_id": app_id}}
        if re.search(r"\b(?:close|quit|exit|hide)\b", normalized):
            return {"action": "os_close_app", "parameters": {"app_id": app_id}}
        if re.search(r"\bminimi[sz]e\b", normalized):
            return {"action": "os_minimize_app", "parameters": {"app_id": app_id}}
        if re.search(r"\bmaximi[sz]e\b", normalized):
            return {"action": "os_maximize_app", "parameters": {"app_id": app_id}}
        if re.search(r"\bpin\b", normalized) and "unpin" not in normalized:
            return {"action": "os_pin_app", "parameters": {"app_id": app_id}}
        if re.search(r"\bunpin\b", normalized):
            return {"action": "os_unpin_app", "parameters": {"app_id": app_id}}

    return None


def _execute_action(user, action_type: str, action_params: dict, command_text: str) -> tuple[bool, str, dict]:
    if action_type == "os_open_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_open_app", {}
        payload = _open_app(user, app_id, command_text)
        return True, f"Opened {payload['window']['title']}.", payload

    if action_type == "os_focus_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_focus_app", {}
        payload = _update_window_state(user, app_id, command_text, action="os_focus_app")
        if not payload:
            return False, f"No open window found for app '{app_id}'.", {}
        return True, f"Focused {payload['window']['title']}.", payload

    if action_type == "os_close_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_close_app", {}
        payload = _close_app(user, app_id, command_text)
        if not payload:
            return False, f"No open window found for app '{app_id}'.", {}
        return True, f"Closed {payload['title']}.", payload

    if action_type == "os_minimize_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_minimize_app", {}
        payload = _update_window_state(
            user,
            app_id,
            command_text,
            action="os_minimize_app",
            is_minimized=True,
        )
        if not payload:
            return False, f"No open window found for app '{app_id}'.", {}
        return True, f"Minimized {payload['window']['title']}.", payload

    if action_type == "os_maximize_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_maximize_app", {}
        payload = _update_window_state(
            user,
            app_id,
            command_text,
            action="os_maximize_app",
            state_data={"isMaximized": True},
        )
        if not payload:
            return False, f"No open window found for app '{app_id}'.", {}
        return True, f"Maximized {payload['window']['title']}.", payload

    if action_type == "os_pin_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_pin_app", {}
        payload = _update_window_state(
            user,
            app_id,
            command_text,
            action="os_pin_app",
            is_pinned=True,
        )
        if not payload:
            payload = _open_app(user, app_id, command_text)
            payload["window"]["is_pinned"] = True
            window = OSAppWindow.objects.get(id=payload["window"]["id"])
            window.is_pinned = True
            window.save(update_fields=["is_pinned", "updated_at"])
            payload["window"] = _serialize_window(window)
            _send_action_event(user.id, "os_pin_app", payload)
        return True, f"Pinned {payload['window']['title']}.", payload

    if action_type == "os_unpin_app":
        app_id = action_params.get("app_id")
        if not app_id:
            return False, "app_id is required for os_unpin_app", {}
        payload = _update_window_state(
            user,
            app_id,
            command_text,
            action="os_unpin_app",
            is_pinned=False,
        )
        if not payload:
            return False, f"No open window found for app '{app_id}'.", {}
        return True, f"Unpinned {payload['window']['title']}.", payload

    if action_type == "os_notify":
        message = action_params.get("message")
        if not message:
            return False, "message is required for os_notify", {}
        payload = _create_notification(user, message, command_text, action_params.get("level", "info"))
        return True, "Notification created.", payload

    if action_type == "os_set_wallpaper":
        wallpaper = action_params.get("wallpaper")
        if not wallpaper:
            return False, "wallpaper is required for os_set_wallpaper", {}
        payload = _set_wallpaper(user, wallpaper, command_text)
        return True, "Wallpaper updated.", payload

    if action_type in BROWSER_TOOL_ALIASES:
        _send_action_event(user.id, action_type, {**action_params, "command_text": command_text})
        execution = _execute_browser_tool(user, action_type, action_params)
        return True, "Browser command resolved.", execution

    _send_action_event(user.id, action_type, {**action_params, "command_text": command_text})
    return True, f"Action {action_type} triggered.", action_params


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def process_context(request):
    """
    Endpoint for the help assistant to receive current frontend context.
    The payload should include what is currently on the screen.
    """
    context_data = request.data.get("context", {})
    workspace = _get_workspace(request.user)

    theme_preferences = dict(workspace.theme_preferences or {})
    theme_preferences["last_context"] = context_data
    workspace.theme_preferences = theme_preferences
    workspace.save(update_fields=["theme_preferences", "updated_at"])

    return Response(
        {
            "status": "success",
            "message": "Context received",
            "received_context": context_data,
            "workspace_id": workspace.id,
        },
        status=status.HTTP_200_OK,
    )


@api_view(["POST"])
@permission_classes([IsAuthenticated])
def trigger_action(request):
    """
    Endpoint for Buddy to issue commands to BrowserOS.

    Supports either:
      1. Explicit actions: {action_type, parameters}
      2. Natural language commands: {command} or {text}
    """
    action_type = request.data.get("action_type")
    action_params = request.data.get("parameters", {}) or {}
    command_text = (request.data.get("command") or request.data.get("text") or "").strip()

    parsed_command = None
    if not action_type and command_text:
        parsed_command = _parse_text_command(command_text)
        if not parsed_command:
            return Response(
                {
                    "status": "error",
                    "message": "Could not understand the command.",
                    "command_text": command_text,
                    "supported_examples": [
                        "open terminal",
                        "focus calculator",
                        "minimize files",
                        "show notification \"Build finished\"",
                        "set wallpaper to aurora gradient",
                        "navigate to https://example.com",
                        "click button.submit",
                        "fill \"hello\" into textarea",
                        "take screenshot \"homepage\"",
                    ],
                },
                status=status.HTTP_400_BAD_REQUEST,
            )
        action_type = parsed_command["action"]
        action_params = parsed_command["parameters"]

    if not action_type:
        return Response(
            {
                "status": "error",
                "message": "action_type or command is required",
            },
            status=status.HTTP_400_BAD_REQUEST,
        )

    ok, message, details = _execute_action(
        request.user,
        action_type,
        action_params,
        command_text or action_type,
    )
    http_status = status.HTTP_200_OK if ok else status.HTTP_400_BAD_REQUEST
    response_status = "success" if ok else "error"

    return Response(
        {
            "status": response_status,
            "message": message,
            "action_details": {
                "type": action_type,
                "params": action_params,
                "resolved_from_text": bool(parsed_command),
                "command_text": command_text or None,
                "details": details,
            },
        },
        status=http_status,
    )
