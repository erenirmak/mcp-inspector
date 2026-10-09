import asyncio
import logging
import os
import threading
from contextlib import asynccontextmanager

from mcp import ClientSession, StdioServerParameters
from mcp.client.sse import sse_client
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client

from .config import (
    MAX_RETRIES,
    RETRY_DELAY,
    STDIO_ALLOWED_COMMANDS,
    STDIO_BLOCKED_ENV_KEYS,
    STDIO_ENABLED,
    TOOL_EXECUTION_TIMEOUT,
)

log = logging.getLogger(__name__)

# Store active sessions and stop events (keyed by connection_id)
active_sessions: dict[str, ClientSession] = {}
stop_events: dict[str, asyncio.Event] = {}

# ---------------------------------------------------------------------------
# Thread-safe URL coordination.
#
# _probing_urls      – URLs currently being health-probed (short-lived).
# _user_session_urls – URL → count of live user sessions on that URL.
#
# Rules:
#   • Health probes acquire the URL slot only when no probe AND no user
#     session is active for that URL.
#   • User connections never block each other — multiple simultaneous
#     connections to the same URL are fully supported.
#   • User connections only pause briefly if a health probe is mid-flight.
# ---------------------------------------------------------------------------
_url_lock = threading.Lock()
_probing_urls: set[str] = set()
_user_session_urls: dict[str, int] = {}  # url → active user-session count
_live_sessions_by_url: dict[str, str] = {}  # url → connection_id of a live user session
_live_session_data: dict[str, dict] = {}  # url → {tools, resources, prompts} from a live session


def _acquire_probe(url: str) -> bool:
    """Try to acquire the URL for a health probe.
    Fails immediately if a probe is already running OR any user session is active."""
    with _url_lock:
        if url in _probing_urls or _user_session_urls.get(url, 0) > 0:
            return False
        _probing_urls.add(url)
        return True


def _release_probe(url: str) -> None:
    with _url_lock:
        _probing_urls.discard(url)


def _add_user_session(url: str, connection_id: str) -> None:
    with _url_lock:
        _user_session_urls[url] = _user_session_urls.get(url, 0) + 1
        # Remember one live session per URL so the registry can reuse its data
        # instead of opening a second connection while the user is connected.
        _live_sessions_by_url.setdefault(url, connection_id)


def _remove_user_session(url: str, connection_id: str | None = None) -> None:
    with _url_lock:
        count = _user_session_urls.get(url, 0) - 1
        if count <= 0:
            _user_session_urls.pop(url, None)
            _live_sessions_by_url.pop(url, None)
            _live_session_data.pop(url, None)
        else:
            _user_session_urls[url] = count
            if connection_id is not None and _live_sessions_by_url.get(url) == connection_id:
                _live_sessions_by_url.pop(url, None)


def get_live_session_data(url: str) -> dict | None:
    """Return the cached tools/resources/prompts of a live user session on *url*.

    The registry uses this to populate a record while the user is interactively
    connected (URL busy) instead of opening a second connection.
    Returns ``None`` when no live session data is available for the URL.
    """
    with _url_lock:
        data = _live_session_data.get(url)
    return data


def is_url_busy(url: str) -> bool:
    """True when a probe is running or at least one user session is active for this URL.
    Used by the server registry to skip probes while the user is interactively connected."""
    with _url_lock:
        return url in _probing_urls or _user_session_urls.get(url, 0) > 0


# ── Stdio validation ──────────────────────────────────────────────────────────


def _validate_stdio(command: str, args: list, env: dict | None) -> tuple[str, list, dict | None]:
    """Validate and sanitise stdio connection parameters.

    Raises ValueError with a user-safe message if the request is not permitted.
    Returns the (possibly normalised) command, args, and env.
    """
    if not STDIO_ENABLED:
        raise ValueError(
            "stdio connections are disabled on this server. Set STDIO_ENABLED=true to enable them."
        )

    # Reject path traversal and absolute paths to restrict to named commands
    basename = os.path.basename(command)
    if not basename or "/" in command or "\\" in command:
        raise ValueError("Command must be a plain executable name without path separators.")

    if STDIO_ALLOWED_COMMANDS and basename not in STDIO_ALLOWED_COMMANDS:
        raise ValueError(f'Command "{basename}" is not in the allowed commands list.')

    # Sanitise environment: drop dangerous keys
    clean_env: dict | None = None
    if env:
        blocked = {k for k in env if k in STDIO_BLOCKED_ENV_KEYS}
        if blocked:
            log.warning("Blocked env keys stripped from stdio request: %s", blocked)
        clean_env = {k: v for k, v in env.items() if k not in STDIO_BLOCKED_ENV_KEYS}

    return command, args, clean_env


def serialize_mcp_data(obj):
    """Convert MCP Pydantic model objects to JSON-serializable dicts."""
    if hasattr(obj, "model_dump"):
        return obj.model_dump()
    elif hasattr(obj, "dict"):
        return obj.dict()
    elif isinstance(obj, list):
        return [serialize_mcp_data(item) for item in obj]
    return obj


def _auth_headers(api_key: str) -> dict:
    """Build an ``Authorization: Bearer <key>`` header dict (empty if no key)."""
    if api_key:
        return {"Authorization": f"Bearer {api_key}"}
    return {}


@asynccontextmanager
async def _http_transport(target: str, conn_type: str, api_key: str = ""):
    """Yield a normalised ``(read, write, _session)`` tuple for the given transport.

    ``sse_client`` yields a 2-tuple while ``streamable_http_client`` yields a
    3-tuple; this wrapper makes both look the same to callers.
    ``api_key`` is sent as an ``Authorization: Bearer`` header when set.
    """
    headers = _auth_headers(api_key)
    if conn_type == "sse":
        async with sse_client(target, headers=headers or None) as (read, write):
            yield read, write, None
    else:
        # streamable_http_client has no ``headers`` parameter — inject the
        # headers via a pre-configured httpx.AsyncClient.
        import httpx

        client = httpx.AsyncClient(headers=headers) if headers else None
        try:
            async with streamable_http_client(
                target, http_client=client
            ) as (read, write, session):
                yield read, write, session
        finally:
            if client is not None:
                await client.aclose()


async def _list_session_data(session: ClientSession) -> dict:
    """Fetch tools/resources/prompts from an initialised session."""
    tools = await session.list_tools()
    resources_list: list = []
    prompts_list: list = []
    try:
        resources_list = (await session.list_resources()).resources
    except Exception:
        pass
    try:
        prompts_list = (await session.list_prompts()).prompts
    except Exception:
        pass
    return {
        "tools": serialize_mcp_data(tools.tools),
        "resources": serialize_mcp_data(resources_list),
        "prompts": serialize_mcp_data(prompts_list),
    }


async def _fetch_server_data(target: str, conn_type: str = "http", api_key: str = ""):
    """Connect to an MCP server, fetch tools/resources/prompts, disconnect.

    Only one probe per URL at a time; skips if a user session is active.
    Waits up to 10 s if another probe is in progress, then raises RuntimeError.
    Retries up to MAX_RETRIES on transient MCP/network errors.
    """
    acquired = False
    for _ in range(10):
        if _acquire_probe(target):
            acquired = True
            break
        await asyncio.sleep(1)

    if not acquired:
        raise RuntimeError(f"Server {target} is busy — skipping probe")

    try:
        last_error = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                async with _http_transport(target, conn_type, api_key) as (read, write, _):
                    async with ClientSession(read, write) as session:
                        await session.initialize()
                        return await _list_session_data(session)
            except Exception as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(RETRY_DELAY * attempt)
        raise last_error  # type: ignore[misc]
    finally:
        _release_probe(target)


class MCPConnectionManager:
    def __init__(self, socketio):
        self.socketio = socketio
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._start_loop, daemon=True)
        self.thread.start()

    def _start_loop(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def connect(
        self,
        connection_id: str,
        conn_type: str,
        target: str,
        env: dict | None = None,
        args: list | None = None,
        sid: str = "",
        api_key: str = "",
    ) -> None:
        """Start an async MCP connection.  *sid* is the SocketIO session ID of the
        requesting client — all events for this connection are emitted only to that client."""
        if args is None:
            args = []
        stop_events[connection_id] = asyncio.Event()
        asyncio.run_coroutine_threadsafe(
            self._connect_async(connection_id, conn_type, target, env, args, sid, api_key),
            self.loop,
        )

    def disconnect(self, connection_id):
        if connection_id in stop_events:
            self.loop.call_soon_threadsafe(stop_events[connection_id].set)

    # ── Tool execution ──────────────────────────────────────────────────────

    def execute_tool(self, connection_id: str, tool_name: str, arguments: dict):
        """Synchronously execute a tool on an active session (for REST callers).

        Blocks the calling thread until the tool returns or times out (30 s).
        Raises KeyError if there is no active session for *connection_id*.
        Raises any exception propagated from the MCP server.
        """
        if connection_id not in active_sessions:
            raise KeyError(f"No active session for connection '{connection_id}'")
        session = active_sessions[connection_id]
        future = asyncio.run_coroutine_threadsafe(
            session.call_tool(tool_name, arguments),
            self.loop,
        )
        result = future.result(timeout=TOOL_EXECUTION_TIMEOUT)
        return serialize_mcp_data(result)

    def execute_tool_async(self, connection_id: str, tool_name: str, arguments: dict, sid: str):
        """Asynchronously execute a tool and push the result to *sid* via socket."""
        asyncio.run_coroutine_threadsafe(
            self._execute_tool_emit(connection_id, tool_name, arguments, sid),
            self.loop,
        )

    async def _execute_tool_emit(self, connection_id: str, tool_name: str, arguments: dict, sid: str):
        if connection_id not in active_sessions:
            self.socketio.emit(
                "tool_result",
                {"tool": tool_name, "error": f"No active session for connection '{connection_id}'"},
                to=sid,
            )
            return
        session = active_sessions[connection_id]
        try:
            result = await session.call_tool(tool_name, arguments)
            self.socketio.emit(
                "tool_result",
                {"tool": tool_name, "result": serialize_mcp_data(result)},
                to=sid,
            )
        except Exception as exc:
            self.socketio.emit(
                "tool_result",
                {"tool": tool_name, "error": str(exc)},
                to=sid,
            )

    async def _connect_async(
        self,
        connection_id: str,
        conn_type: str,
        target: str,
        env: dict | None,
        args: list,
        sid: str,
        api_key: str = "",
    ) -> None:
        try:
            self.socketio.emit("log", {"id": connection_id, "message": f"Connecting to {target}..."}, to=sid)

            if conn_type == "stdio":
                command, args, env = _validate_stdio(target, args, env)
                await self._connect_stdio(connection_id, command, env, args, sid)
            elif conn_type in ("sse", "http"):
                await self._connect_http(connection_id, target, sid, conn_type, api_key)
            else:
                raise ValueError(f"Unknown connection type: {conn_type!r}")

        except ValueError as e:
            # User-visible validation errors
            self.socketio.emit("connection_error", {"id": connection_id, "error": str(e)}, to=sid)
        except Exception:
            log.exception("Unexpected error in MCP connection %s to %s", connection_id, target)
            self.socketio.emit(
                "connection_error",
                {"id": connection_id, "error": "Internal connection error — see server logs."},
                to=sid,
            )
        finally:
            if connection_id in active_sessions:
                del active_sessions[connection_id]
            if connection_id in stop_events:
                del stop_events[connection_id]
            # Always notify the owner client
            self.socketio.emit("connection_closed", {"id": connection_id}, to=sid)

    async def _connect_stdio(
        self, connection_id: str, target: str, env: dict | None, args: list, sid: str
    ) -> None:
        server_params = StdioServerParameters(command=target, args=args, env=env)
        async with stdio_client(server_params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                active_sessions[connection_id] = session

                data = await _list_session_data(session)
                self.socketio.emit("connection_success", {"id": connection_id, "data": data}, to=sid)

                if connection_id in stop_events:
                    await stop_events[connection_id].wait()
                self.socketio.emit("log", {"id": connection_id, "message": "Connection closed."}, to=sid)

    async def _connect_http(
        self, connection_id: str, target: str, sid: str, conn_type: str = "http", api_key: str = ""
    ) -> None:
        for wait in range(10):
            with _url_lock:
                if target not in _probing_urls:
                    break
            self.socketio.emit(
                "log",
                {"id": connection_id, "message": f"Health probe in progress, waiting... ({wait + 1}s)"},
                to=sid,
            )
            await asyncio.sleep(1)

        _add_user_session(target, connection_id)
        try:
            last_error: Exception | None = None
            for attempt in range(1, MAX_RETRIES + 1):
                try:
                    async with _http_transport(target, conn_type, api_key) as (read, write, _):
                        async with ClientSession(read, write) as session:
                            await session.initialize()
                            active_sessions[connection_id] = session

                            data = await _list_session_data(session)
                            # Cache so the registry can populate a record while
                            # this session is live (URL busy) without a 2nd connect.
                            with _url_lock:
                                _live_session_data[target] = data
                            self.socketio.emit(
                                "connection_success", {"id": connection_id, "data": data}, to=sid
                            )

                            if connection_id in stop_events:
                                await stop_events[connection_id].wait()
                            self.socketio.emit(
                                "log", {"id": connection_id, "message": "Connection closed."}, to=sid
                            )
                            return
                except Exception as e:
                    last_error = e
                    if attempt < MAX_RETRIES:
                        self.socketio.emit(
                            "log",
                            {
                                "id": connection_id,
                                "message": f"Attempt {attempt} failed, retrying in {RETRY_DELAY * attempt}s...",
                            },
                            to=sid,
                        )
                        await asyncio.sleep(RETRY_DELAY * attempt)
            raise last_error  # type: ignore[misc]
        finally:
            _remove_user_session(target, connection_id)


# Singleton
manager = None


def init_manager(socketio):
    global manager
    manager = MCPConnectionManager(socketio)
    return manager
