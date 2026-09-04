import logging

from django.apps import AppConfig


class _StartingServerFilter(logging.Filter):
    """Drop the SDK's parse error for `@isaacphi/mcp-gdrive`'s banner.

    That server writes `Starting server` to stdout before the MCP handshake,
    which is not JSON-RPC, so `mcp.client.stdio` logs "Failed to parse JSONRPC
    message" with a full traceback and carries on. Migration 0014 verified this
    is harmless, but it looks alarming and the traceback per spawn is pure log
    I/O on an already-loaded box. Anything that is not that one banner line
    still passes through.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            if "Failed to parse JSONRPC message" not in record.getMessage():
                return True
            exc = record.exc_info[1] if record.exc_info else None
            if exc is not None and "Starting server" in str(exc):
                return False
            # The banner text can also arrive as a log arg rather than in the
            # exception, depending on SDK version.
            for arg in (record.args or ()):
                if "Starting server" in str(arg):
                    return False
        except Exception:  # noqa: BLE001 — a logging filter must never raise
            return True
        return True


class McpIntegrationConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'mcp_integration'

    def ready(self) -> None:
        logging.getLogger("mcp.client.stdio").addFilter(_StartingServerFilter())
