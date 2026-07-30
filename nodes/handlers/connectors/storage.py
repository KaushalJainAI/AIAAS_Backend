"""Storage and calendar connectors: Google Drive, Google Calendar, Dropbox, S3."""
from __future__ import annotations

import datetime
import hashlib
import hmac
from typing import Any
from urllib.parse import quote

from ..base import FieldConfig, FieldType
from ..rest_base import ConnectorError, RestConnectorNode



class GoogleDriveNode(RestConnectorNode):
    """Files in Google Drive."""

    node_type = "google_drive"
    name = "Google Drive"
    description = "List, search, copy and delete Google Drive files"
    icon = "📁"
    color = "#1fa463"

    credential_slug = "google-oauth2"
    credential_key = "access_token"
    auth_style = "bearer"
    base_url = "https://www.googleapis.com/drive/v3"

    fields = [
        FieldConfig(name="credential", label="Google Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="google-oauth2"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["list_files", "search_files", "get_file",
                             "create_folder", "delete_file", "share_file"],
                    default="list_files"),
        FieldConfig(name="query", label="Search Query", field_type=FieldType.STRING,
                    required=False, placeholder="name contains 'report'"),
        FieldConfig(name="file_id", label="File ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="folder_name", label="Folder Name", field_type=FieldType.STRING, required=False),
        FieldConfig(name="parent_id", label="Parent Folder ID", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="share_email", label="Share With", field_type=FieldType.STRING, required=False),
        FieldConfig(name="role", label="Role", field_type=FieldType.SELECT,
                    options=["reader", "writer", "commenter"], required=False, default="reader"),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=50),
    ]
    static_output_fields = ["id", "name", "mimeType", "webViewLink"]

    async def run_operation(self, operation, config, secret, context):
        limit = min(int(config.get("limit") or 50), 100)
        file_fields = "id,name,mimeType,webViewLink,modifiedTime,size"

        if operation in ("list_files", "search_files"):
            params: dict[str, Any] = {
                "pageSize": limit,
                "fields": f"files({file_fields}),nextPageToken",
            }
            if operation == "search_files":
                query = config.get("query", "").strip()
                if not query:
                    raise ConnectorError("A search query is required.")
                params["q"] = query
            elif config.get("parent_id"):
                params["q"] = f"'{config['parent_id']}' in parents and trashed = false"
            else:
                params["q"] = "trashed = false"
            data = await self.call("GET", "/files", secret=secret, params=params)
            return (data or {}).get("files", [])

        if operation == "get_file":
            file_id = config.get("file_id", "").strip()
            if not file_id:
                raise ConnectorError("File ID is required.")
            return await self.call("GET", f"/files/{file_id}", secret=secret,
                                   params={"fields": file_fields})

        if operation == "create_folder":
            folder_name = config.get("folder_name", "").strip()
            if not folder_name:
                raise ConnectorError("Folder name is required.")
            body: dict[str, Any] = {
                "name": folder_name,
                "mimeType": "application/vnd.google-apps.folder",
            }
            if config.get("parent_id"):
                body["parents"] = [config["parent_id"]]
            return await self.call("POST", "/files", secret=secret, json_body=body)

        if operation == "delete_file":
            file_id = config.get("file_id", "").strip()
            if not file_id:
                raise ConnectorError("File ID is required.")
            await self.call("DELETE", f"/files/{file_id}", secret=secret)
            return {"id": file_id, "deleted": True}

        if operation == "share_file":
            file_id = config.get("file_id", "").strip()
            email = config.get("share_email", "").strip()
            if not file_id or not email:
                raise ConnectorError("File ID and an email address are required.")
            return await self.call(
                "POST", f"/files/{file_id}/permissions", secret=secret,
                json_body={"type": "user", "role": config.get("role") or "reader",
                           "emailAddress": email},
            )

        raise NotImplementedError(operation)


class GoogleCalendarNode(RestConnectorNode):
    """Events in Google Calendar."""

    node_type = "google_calendar"
    name = "Google Calendar"
    description = "Create, list, update and delete calendar events"
    icon = "📅"
    color = "#4285f4"

    credential_slug = "google-oauth2"
    credential_key = "access_token"
    auth_style = "bearer"
    base_url = "https://www.googleapis.com/calendar/v3"

    fields = [
        FieldConfig(name="credential", label="Google Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="google-oauth2"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["create_event", "list_events", "get_event",
                             "update_event", "delete_event"],
                    default="create_event"),
        FieldConfig(name="calendar_id", label="Calendar ID", field_type=FieldType.STRING,
                    required=False, default="primary"),
        FieldConfig(name="event_id", label="Event ID", field_type=FieldType.STRING, required=False),
        FieldConfig(name="summary", label="Title", field_type=FieldType.STRING, required=False),
        FieldConfig(name="description", label="Description", field_type=FieldType.STRING, required=False),
        FieldConfig(name="start_time", label="Start", field_type=FieldType.STRING,
                    required=False, placeholder="2026-08-15T10:00:00Z"),
        FieldConfig(name="end_time", label="End", field_type=FieldType.STRING,
                    required=False, placeholder="2026-08-15T11:00:00Z"),
        FieldConfig(name="attendees", label="Attendees", field_type=FieldType.STRING,
                    required=False, description="Comma-separated email addresses"),
        FieldConfig(name="time_min", label="List From", field_type=FieldType.STRING, required=False),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=25),
    ]
    static_output_fields = ["id", "summary", "htmlLink", "start", "end"]

    async def run_operation(self, operation, config, secret, context):
        calendar_id = quote(config.get("calendar_id") or "primary", safe="")

        if operation == "list_events":
            # Default to "from now" rather than the beginning of the calendar:
            # listing events opens on last year's meetings otherwise.
            time_min = config.get("time_min") or datetime.datetime.now(
                datetime.timezone.utc
            ).isoformat().replace("+00:00", "Z")
            data = await self.call(
                "GET", f"/calendars/{calendar_id}/events", secret=secret,
                params={
                    "timeMin": time_min,
                    "maxResults": min(int(config.get("limit") or 25), 250),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                },
            )
            return (data or {}).get("items", [])

        if operation == "get_event":
            event_id = config.get("event_id", "").strip()
            if not event_id:
                raise ConnectorError("Event ID is required.")
            return await self.call("GET", f"/calendars/{calendar_id}/events/{event_id}",
                                   secret=secret)

        if operation == "delete_event":
            event_id = config.get("event_id", "").strip()
            if not event_id:
                raise ConnectorError("Event ID is required.")
            await self.call("DELETE", f"/calendars/{calendar_id}/events/{event_id}",
                            secret=secret)
            return {"id": event_id, "deleted": True}

        summary = config.get("summary", "").strip()
        start = config.get("start_time", "").strip()
        end = config.get("end_time", "").strip()

        if operation == "create_event":
            if not summary or not start or not end:
                raise ConnectorError("Title, start and end are required to create an event.")
            body: dict[str, Any] = {
                "summary": summary,
                "start": {"dateTime": start},
                "end": {"dateTime": end},
            }
            if config.get("description"):
                body["description"] = config["description"]
            if config.get("attendees"):
                body["attendees"] = [
                    {"email": e.strip()} for e in config["attendees"].split(",") if e.strip()
                ]
            return await self.call("POST", f"/calendars/{calendar_id}/events",
                                   secret=secret, json_body=body)

        if operation == "update_event":
            event_id = config.get("event_id", "").strip()
            if not event_id:
                raise ConnectorError("Event ID is required.")
            body = {}
            if summary:
                body["summary"] = summary
            if config.get("description"):
                body["description"] = config["description"]
            if start:
                body["start"] = {"dateTime": start}
            if end:
                body["end"] = {"dateTime": end}
            if not body:
                raise ConnectorError("Nothing to update.")
            # PATCH, not PUT: PUT replaces the event and silently drops every
            # field not restated, including attendees.
            return await self.call("PATCH", f"/calendars/{calendar_id}/events/{event_id}",
                                   secret=secret, json_body=body)

        raise NotImplementedError(operation)


class DropboxNode(RestConnectorNode):
    """Files in Dropbox."""

    node_type = "dropbox"
    name = "Dropbox"
    description = "List, search, move and delete Dropbox files"
    icon = "📦"
    color = "#0061ff"

    credential_slug = "dropbox"
    credential_key = "accessToken"
    auth_style = "bearer"
    base_url = "https://api.dropboxapi.com/2"

    fields = [
        FieldConfig(name="credential", label="Dropbox Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="dropbox"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["list_folder", "search", "create_folder",
                             "delete", "move", "get_temporary_link"],
                    default="list_folder"),
        FieldConfig(name="path", label="Path", field_type=FieldType.STRING,
                    required=False, placeholder="/Reports"),
        FieldConfig(name="to_path", label="Destination Path", field_type=FieldType.STRING,
                    required=False),
        FieldConfig(name="query", label="Search Query", field_type=FieldType.STRING, required=False),
        FieldConfig(name="limit", label="Limit", field_type=FieldType.NUMBER, required=False, default=100),
    ]
    static_output_fields = ["name", "path_lower", ".tag", "size"]

    @staticmethod
    def _norm(path: str) -> str:
        """Dropbox wants a leading slash, and "" for the root — never "/"."""
        path = (path or "").strip()
        if not path or path == "/":
            return ""
        return path if path.startswith("/") else f"/{path}"

    async def run_operation(self, operation, config, secret, context):
        path = self._norm(config.get("path", ""))

        if operation == "list_folder":
            data = await self.call("POST", "/files/list_folder", secret=secret,
                                   json_body={"path": path,
                                              "limit": min(int(config.get("limit") or 100), 2000)})
            return (data or {}).get("entries", [])

        if operation == "search":
            query = config.get("query", "").strip()
            if not query:
                raise ConnectorError("A search query is required.")
            data = await self.call(
                "POST", "/files/search_v2", secret=secret,
                json_body={"query": query,
                           "options": {"max_results": min(int(config.get("limit") or 100), 1000)}},
            )
            return [m.get("metadata", {}).get("metadata", {})
                    for m in (data or {}).get("matches", [])]

        if operation == "create_folder":
            if not path:
                raise ConnectorError("A path is required.")
            data = await self.call("POST", "/files/create_folder_v2", secret=secret,
                                   json_body={"path": path})
            return (data or {}).get("metadata", {})

        if operation == "delete":
            if not path:
                raise ConnectorError("A path is required.")
            data = await self.call("POST", "/files/delete_v2", secret=secret,
                                   json_body={"path": path})
            return (data or {}).get("metadata", {})

        if operation == "move":
            to_path = self._norm(config.get("to_path", ""))
            if not path or not to_path:
                raise ConnectorError("Both a source and a destination path are required.")
            data = await self.call("POST", "/files/move_v2", secret=secret,
                                   json_body={"from_path": path, "to_path": to_path})
            return (data or {}).get("metadata", {})

        if operation == "get_temporary_link":
            if not path:
                raise ConnectorError("A path is required.")
            return await self.call("POST", "/files/get_temporary_link", secret=secret,
                                   json_body={"path": path})

        raise NotImplementedError(operation)


class S3Node(RestConnectorNode):
    """Objects in Amazon S3."""

    node_type = "aws_s3"
    name = "AWS S3"
    description = "List, read, write and delete S3 objects"
    icon = "🪣"
    color = "#569a31"

    credential_slug = "aws"
    auth_style = "none"  # SigV4 is computed per request, not a static header.

    fields = [
        FieldConfig(name="credential", label="AWS Credential", field_type=FieldType.CREDENTIAL,
                    credential_type="aws"),
        FieldConfig(name="operation", label="Operation", field_type=FieldType.SELECT,
                    options=["list_objects", "get_object", "put_object", "delete_object"],
                    default="list_objects"),
        FieldConfig(name="bucket", label="Bucket", field_type=FieldType.STRING),
        FieldConfig(name="key", label="Object Key", field_type=FieldType.STRING, required=False),
        FieldConfig(name="body", label="Content", field_type=FieldType.STRING, required=False,
                    description="Body for put_object"),
        FieldConfig(name="prefix", label="Prefix", field_type=FieldType.STRING, required=False),
        FieldConfig(name="region", label="Region", field_type=FieldType.STRING,
                    required=False, default="ap-south-1"),
        FieldConfig(name="limit", label="Max Keys", field_type=FieldType.NUMBER,
                    required=False, default=100),
    ]
    static_output_fields = ["key", "size", "last_modified", "etag"]

    @staticmethod
    def _sigv4_headers(
        method: str, host: str, canonical_uri: str, query: str,
        payload: bytes, access_key: str, secret_key: str, region: str,
        session_token: str = "",
    ) -> dict[str, str]:
        """
        Sign a request with AWS Signature V4.

        Written out rather than pulled from boto3 because boto3 is synchronous
        and this path is async; wrapping a sync client per call would put a
        thread behind every S3 operation. The signing itself is deterministic and
        well specified, so it is a reasonable thing to do directly.
        """
        service = "s3"
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()

        headers = {
            "host": host,
            "x-amz-content-sha256": payload_hash,
            "x-amz-date": amz_date,
        }
        if session_token:
            headers["x-amz-security-token"] = session_token

        signed_headers = ";".join(sorted(headers))
        canonical_headers = "".join(f"{k}:{headers[k]}\n" for k in sorted(headers))
        canonical_request = (
            f"{method}\n{canonical_uri}\n{query}\n"
            f"{canonical_headers}\n{signed_headers}\n{payload_hash}"
        )

        scope = f"{date_stamp}/{region}/{service}/aws4_request"
        to_sign = (
            f"AWS4-HMAC-SHA256\n{amz_date}\n{scope}\n"
            f"{hashlib.sha256(canonical_request.encode()).hexdigest()}"
        )

        def _sign(key: bytes, msg: str) -> bytes:
            return hmac.new(key, msg.encode(), hashlib.sha256).digest()

        k_date = _sign(f"AWS4{secret_key}".encode(), date_stamp)
        k_region = _sign(k_date, region)
        k_service = _sign(k_region, service)
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, to_sign.encode(), hashlib.sha256).hexdigest()

        headers["Authorization"] = (
            f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return headers

    async def run_operation(self, operation, config, secret, context):
        creds = await context.get_credential(config.get("credential")) or {}
        access_key = (creds.get("accessKeyId") or creds.get("access_key_id") or "").strip()
        secret_key = (creds.get("secretAccessKey") or creds.get("secret_access_key") or "").strip()
        session_token = (creds.get("sessionToken") or creds.get("session_token") or "").strip()
        if not access_key or not secret_key:
            raise ConnectorError("AWS credential needs an access key ID and a secret access key.")

        region = (config.get("region") or creds.get("region") or "ap-south-1").strip()
        bucket = config.get("bucket", "").strip()
        if not bucket:
            raise ConnectorError("Bucket is required.")

        host = f"{bucket}.s3.{region}.amazonaws.com"
        key = config.get("key", "").strip().lstrip("/")

        if operation == "list_objects":
            limit = min(int(config.get("limit") or 100), 1000)
            prefix = config.get("prefix", "")
            # Query string must be sorted and encoded exactly as signed.
            pairs = [("list-type", "2"), ("max-keys", str(limit))]
            if prefix:
                pairs.append(("prefix", prefix))
            query = "&".join(f"{k}={quote(v, safe='')}" for k, v in sorted(pairs))
            headers = self._sigv4_headers("GET", host, "/", query, b"", access_key,
                                          secret_key, region, session_token)
            body = await self.call("GET", f"https://{host}/?{query}", headers=headers)
            # S3 answers XML; request_json falls back to {"text": ...}.
            return self._parse_list_xml(body.get("text", "") if isinstance(body, dict) else "")

        if not key:
            raise ConnectorError("Object key is required.")
        canonical_uri = "/" + quote(key, safe="/")

        if operation == "get_object":
            headers = self._sigv4_headers("GET", host, canonical_uri, "", b"", access_key,
                                          secret_key, region, session_token)
            body = await self.call("GET", f"https://{host}{canonical_uri}", headers=headers)
            content = body.get("text") if isinstance(body, dict) else body
            return {"key": key, "bucket": bucket, "content": content}

        if operation == "put_object":
            payload = (config.get("body") or "").encode()
            headers = self._sigv4_headers("PUT", host, canonical_uri, "", payload, access_key,
                                          secret_key, region, session_token)
            await self.call("PUT", f"https://{host}{canonical_uri}", headers=headers,
                            data=payload)
            return {"key": key, "bucket": bucket, "written": len(payload)}

        if operation == "delete_object":
            headers = self._sigv4_headers("DELETE", host, canonical_uri, "", b"", access_key,
                                          secret_key, region, session_token)
            await self.call("DELETE", f"https://{host}{canonical_uri}", headers=headers)
            return {"key": key, "bucket": bucket, "deleted": True}

        raise NotImplementedError(operation)

    @staticmethod
    def _parse_list_xml(xml_text: str) -> list[dict]:
        """Pull the object list out of an S3 ListBucketResult."""
        import xml.etree.ElementTree as ET
        if not xml_text:
            return []
        try:
            root = ET.fromstring(xml_text)
        except ET.ParseError:
            return []
        ns = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}
        out = []
        for item in root.findall("s3:Contents", ns) or root.findall("Contents"):
            def _text(tag: str) -> str:
                node = item.find(f"s3:{tag}", ns)
                if node is None:
                    node = item.find(tag)
                return node.text if node is not None and node.text else ""
            out.append({
                "key": _text("Key"),
                "size": int(_text("Size") or 0),
                "last_modified": _text("LastModified"),
                "etag": _text("ETag").strip('"'),
            })
        return out


__all__ = ["GoogleDriveNode", "GoogleCalendarNode", "DropboxNode", "S3Node"]
