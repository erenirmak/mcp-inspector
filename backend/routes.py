import logging
import math
import uuid

from flask import Blueprint, jsonify, render_template, request, session

from .config import validate_server_url
from .mcp_manager import active_sessions, serialize_mcp_data
from .server_registry import registry
from .tool_search import tool_index

log = logging.getLogger(__name__)

main_bp = Blueprint("main", __name__)

# ── Health endpoint ──────────────────────────────────────────────────────


@main_bp.route("/healthz")
def healthz():
    """Lightweight liveness / readiness probe."""
    from .db import get_db

    db_status = "ok"
    try:
        with get_db() as db:
            db.execute(__import__("sqlalchemy").text("SELECT 1"))
    except Exception:
        log.exception("Health check DB ping failed")
        db_status = "error"
    return jsonify(
        {
            "status": "ok",
            "db": db_status,
            "servers": len(registry.list_servers()),
        }
    ), 200 if db_status == "ok" else 503


# ── Pages ────────────────────────────────────────────────────────────


@main_bp.route("/")
def index():
    return render_template("index.html")


# ── Server Registry API ─────────────────────────────────────────────


@main_bp.route("/api/servers", methods=["GET"])
def list_servers():
    """List all registered MCP servers + their status."""
    return jsonify(registry.list_servers())


@main_bp.route("/api/servers", methods=["POST"])
def add_server():
    """Register a new MCP server to track.
    Body: { "name": "...", "url": "...", "type": "http" }
    For stdio: { "name": "...", "type": "stdio", "command": "...", "args": [...] }
    """
    data = request.json or {}
    name = data.get("name", "").strip()
    url = data.get("url", "").strip()
    conn_type = data.get("type", "http")
    command = data.get("command", "").strip()
    args = data.get("args", []) or []

    if not name:
        return jsonify({"error": "name is required"}), 400

    if conn_type == "stdio":
        if not command:
            return jsonify({"error": "command is required for stdio servers"}), 400
        if not isinstance(args, list):
            return jsonify({"error": "args must be a list"}), 400
        # Reuse the manager's stdio validation (STDIO_ENABLED, allow-list, paths)
        from .mcp_manager import _validate_stdio

        try:
            command, args, _ = _validate_stdio(command, args, None)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
    else:
        if not url:
            return jsonify({"error": "url is required"}), 400
        # SSRF protection: reject private / loopback / link-local URLs
        try:
            validate_server_url(url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        # Reject duplicate URLs (case-insensitive, trailing-slash-insensitive)
        existing = registry.find_by_url(url)
        if existing is not None:
            return (
                jsonify(
                    {
                        "error": f"A server with this URL is already registered as \"{existing.name}\".",
                        "existing_id": existing.id,
                    }
                ),
                409,
            )

    server_id = str(uuid.uuid4())
    rec = registry.add_server(server_id, name, url, conn_type, command, args)
    _rebuild_search_index()
    return jsonify(rec.to_dict()), 201


@main_bp.route("/api/servers/<server_id>", methods=["DELETE"])
def remove_server(server_id):
    """Remove a server from the registry."""
    found = registry.remove_server(server_id)
    if not found:
        return jsonify({"error": "server not found"}), 404
    _rebuild_search_index()
    return jsonify({"ok": True})


@main_bp.route("/api/servers/<server_id>/refresh", methods=["POST"])
def refresh_server(server_id):
    """Force re-probe a single server."""
    srv = registry.get_server(server_id)
    if not srv:
        return jsonify({"error": "server not found"}), 404
    registry.refresh_server(server_id)
    return jsonify({"ok": True, "message": "Refresh triggered"})


@main_bp.route("/api/servers/refresh", methods=["POST"])
def refresh_all_servers():
    """Force re-probe all servers."""
    registry.refresh_all()
    return jsonify({"ok": True, "message": "Refresh triggered for all servers"})


# ── Tool Search API ─────────────────────────────────────────────────


@main_bp.route("/api/tools", methods=["GET"])
def list_all_tools():
    """Return tools from all registered servers with optional pagination.
    Query params: page=1 (1-based), page_size=50
    """
    all_tools = registry.get_all_tools()
    return _paginate(all_tools, label="tools")


@main_bp.route("/api/tools/search", methods=["GET"])
def search_tools():
    """Search tools by keyword / sentence.
    Query params: q=<query>&k=<top_k, default 10>&page=1&page_size=50
    Returns the top-k most relevant tools across all registered servers.
    """
    query = request.args.get("q", "").strip()
    top_k = request.args.get("k", "10")

    try:
        top_k = max(1, min(int(top_k), 100))
    except ValueError:
        top_k = 10

    if not query:
        return jsonify({"error": "query parameter 'q' is required"}), 400

    results = tool_index.search(query, top_k=top_k)
    paginated = _paginate(results, label="tools")
    data = paginated.get_json()
    data["query"] = query
    return jsonify(data)


# ── Active Connections API ──────────────────────────────────────────


@main_bp.route("/api/connections", methods=["GET"])
def list_connections():
    """List MCP connection IDs owned by the current browser session."""
    from .socket_events import connection_owners

    browser_id = session.get("browser_id")
    if browser_id:
        owned = [cid for cid, bid in connection_owners.items() if bid == browser_id]
    else:
        owned = []
    return jsonify({"connections": owned})


@main_bp.route("/api/connections/<connection_id>/tools/execute", methods=["POST"])
def rest_execute_tool(connection_id):
    """Execute a tool on an active MCP connection (REST interface).

    Body: { "tool_name": "...", "arguments": { ... } }
    Returns: { "result": { ... } }  or  { "error": "..." }
    """
    from .mcp_manager import manager  # local import to get live singleton
    from .socket_events import connection_owners

    if not manager:
        return jsonify({"error": "MCP Manager not initialised"}), 503

    if connection_id not in active_sessions:
        return jsonify({"error": f"No active session for connection '{connection_id}'"}), 404

    # Ownership check
    browser_id = session.get("browser_id")
    owner = connection_owners.get(connection_id)
    if owner and browser_id and owner != browser_id:
        log.warning(
            "REST ownership violation: browser %s tried to execute on connection %s owned by %s",
            browser_id,
            connection_id,
            owner,
        )
        return jsonify({"error": "Permission denied."}), 403

    data = request.json or {}
    tool_name = data.get("tool_name", "").strip()
    arguments = data.get("arguments") or {}

    if not tool_name:
        return jsonify({"error": "tool_name is required"}), 400

    try:
        result = manager.execute_tool(connection_id, tool_name, arguments)
        return jsonify({"tool": tool_name, "result": result})
    except KeyError as exc:
        return jsonify({"error": str(exc)}), 404
    except TimeoutError:
        return jsonify({"error": "Tool execution timed out"}), 504
    except Exception:
        log.exception("Tool execution failed for connection %s / tool %s", connection_id, tool_name)
        return jsonify({"error": "Tool execution failed — see server logs."}), 500


# ── helpers ──────────────────────────────────────────────────────────────


def _rebuild_search_index():
    """Rebuild the TF-IDF index from all registered server tools."""
    all_tools = registry.get_all_tools()
    if all_tools:
        tool_index.rebuild(all_tools)


def _paginate(items: list, label: str = "items"):
    """Return a paginated JSON response.
    Query params: page (1-based, default 1), page_size (default 50, max 200).
    """
    try:
        page = max(1, int(request.args.get("page", 1)))
    except ValueError:
        page = 1
    try:
        page_size = max(1, min(int(request.args.get("page_size", 50)), 200))
    except ValueError:
        page_size = 50

    total = len(items)
    pages = math.ceil(total / page_size) if page_size else 1
    start = (page - 1) * page_size
    end = start + page_size
    return jsonify(
        {
            label: items[start:end],
            "total": total,
            "page": page,
            "page_size": page_size,
            "pages": pages,
        }
    )
