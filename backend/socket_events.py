import logging
import uuid

from flask import request, session
from flask_socketio import emit

from .mcp_manager import init_manager

log = logging.getLogger(__name__)

# Map each socket session ID to the SET of MCP connection IDs it owns.
# A single browser tab can hold multiple simultaneous MCP sessions.
sid_to_connections: dict[str, set] = {}

# Browser session isolation state (module-level so routes.py can import them)
_sid_to_browser: dict[str, str] = {}  # socket sid → browser_id
connection_owners: dict[str, str] = {}  # connection_id → browser_id


def register_socket_events(socketio):
    manager = init_manager(socketio)

    @socketio.on("connect")
    def handle_connect():
        browser_id = session.get("browser_id")
        if not browser_id:
            # Session may not have been initialised yet (e.g. first connection
            # before before_request ran, or reconnect after a restart).  Assign
            # a fresh id rather than refusing the connection outright.
            browser_id = uuid.uuid4().hex
            session["browser_id"] = browser_id
            log.info("Assigned new browser_id %s to socket %s", browser_id, request.sid)

        _sid_to_browser[request.sid] = browser_id
        sid_to_connections[request.sid] = set()
        emit("log", {"message": "Client connected to Inspector Backend"})

    @socketio.on("inspect_server")
    def handle_inspect(data):
        sid = request.sid
        browser_id = _sid_to_browser.get(sid)
        if not browser_id:
            emit("connection_error", {"error": "Session not recognised — please refresh the page."})
            return

        connection_type = data.get("type", "stdio")
        target = data.get("target")
        env = data.get("env", {})
        args = data.get("args", [])
        api_key = data.get("api_key", "")
        connection_id = data.get("id") or str(uuid.uuid4())

        # For registered servers, use the API key stored in the registry
        # (the client only sends a key for ad-hoc Quick Connect sessions).
        registered_server_id = data.get("registered_server_id")
        if registered_server_id and connection_type in ("http", "sse"):
            from .server_registry import registry

            rec = registry.get_server(registered_server_id)
            if rec is not None:
                api_key = rec.api_key

        # Register this connection under the current socket session and browser
        sid_to_connections.setdefault(sid, set()).add(connection_id)
        connection_owners[connection_id] = browser_id

        if manager:
            manager.connect(connection_id, connection_type, target, env, args, sid=sid, api_key=api_key)
        else:
            emit("connection_error", {"id": connection_id, "error": "MCP Manager not initialized"})

    @socketio.on("disconnect_server")
    def handle_disconnect_server(data):
        connection_id = data.get("id")
        if not connection_id:
            return

        # Ownership check — only the owning browser session may disconnect
        sid = request.sid
        owner = connection_owners.get(connection_id)
        caller = _sid_to_browser.get(sid)
        if owner and caller and owner != caller:
            log.warning(
                "Ownership violation: %s tried to disconnect connection %s owned by %s",
                caller,
                connection_id,
                owner,
            )
            emit("connection_error", {"id": connection_id, "error": "Permission denied."})
            return

        if manager:
            manager.disconnect(connection_id)
        sid_to_connections.get(sid, set()).discard(connection_id)
        connection_owners.pop(connection_id, None)

    @socketio.on("disconnect")
    def handle_disconnect():
        sid = request.sid
        _sid_to_browser.pop(sid, None)
        connection_ids = sid_to_connections.pop(sid, set())
        for cid in connection_ids:
            if manager:
                manager.disconnect(cid)
            connection_owners.pop(cid, None)

    @socketio.on("execute_tool")
    def handle_execute_tool(data):
        """Execute a tool on the specified MCP session.

        Payload: { tool_name: str, arguments: dict, connection_id: str }
        Emits back:  tool_result  { tool: str, result?: ..., error?: str }
        """
        sid = request.sid
        browser_id = _sid_to_browser.get(sid)
        tool_name = data.get("tool_name", "").strip()
        arguments = data.get("arguments") or {}
        connection_id = data.get("connection_id")

        # Fall back to any active connection for this socket
        if not connection_id:
            cids = sid_to_connections.get(sid, set())
            connection_id = next(iter(cids), None)

        if not tool_name:
            emit("tool_result", {"error": "tool_name is required"})
            return

        if not connection_id:
            emit(
                "tool_result",
                {"tool": tool_name, "error": "No active connection. Connect to an MCP server first."},
            )
            return

        # Ownership check
        owner = connection_owners.get(connection_id)
        if owner and browser_id and owner != browser_id:
            log.warning(
                "Ownership violation: %s tried to execute tool on connection %s owned by %s",
                browser_id,
                connection_id,
                owner,
            )
            emit("tool_result", {"tool": tool_name, "error": "Permission denied."})
            return

        if manager:
            manager.execute_tool_async(connection_id, tool_name, arguments, sid)
        else:
            emit("tool_result", {"tool": tool_name, "error": "MCP Manager not initialized"})
