import logging
import os

from dotenv import load_dotenv
from flask import Flask, session
from flask_socketio import SocketIO

# Load .env before importing config so env vars are available
load_dotenv()

from .config import (  # noqa: E402
    CORS_ALLOWED_ORIGINS,
    DATABASE_URL,
    MAX_CONTENT_LENGTH,
    SECRET_KEY,
)
from .db import init_db
from .routes import main_bp
from .socket_events import register_socket_events


def configure_logging() -> None:
    """Set up structured logging for the whole application."""
    log_level = os.environ.get('LOG_LEVEL', 'INFO').upper()
    logging.basicConfig(
        level=getattr(logging, log_level, logging.INFO),
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
        datefmt='%Y-%m-%dT%H:%M:%S',
    )
    # Quieten noisy third-party loggers
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    logging.getLogger('engineio').setLevel(logging.WARNING)
    logging.getLogger('socketio').setLevel(logging.WARNING)


log = logging.getLogger(__name__)

socketio = SocketIO()


def create_app() -> Flask:
    configure_logging()

    app = Flask(
        __name__,
        template_folder='../frontend/templates',
        static_folder='../frontend/static',
    )

    # ── Security ────────────────────────────────────────────────────────────
    app.config['SECRET_KEY'] = SECRET_KEY
    app.config['SESSION_COOKIE_HTTPONLY'] = True
    app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
    # Set Secure=True in production (requires HTTPS)
    app.config['SESSION_COOKIE_SECURE'] = os.environ.get('SESSION_COOKIE_SECURE', 'false').lower() == 'true'

    # ── Request limits ───────────────────────────────────────────────────────
    app.config['MAX_CONTENT_LENGTH'] = MAX_CONTENT_LENGTH

    # ── Database ─────────────────────────────────────────────────────────────
    init_db(DATABASE_URL)
    log.info('Database initialised.')

    # Restore persisted servers now that the DB is ready
    from .server_registry import registry as _registry
    _registry.load_from_db()

    # Prime the search index with any tools that were persisted in the DB
    from .tool_search import tool_index as _tool_index
    _all_tools = _registry.get_all_tools()
    if _all_tools:
        _tool_index.rebuild(_all_tools)
        log.info('Search index primed with %d tool(s) from DB.', len(_all_tools))

    # Kick off an immediate probe so tool data is fresh without waiting 60 s
    _registry.refresh_all()

    # ── Before-request: ensure every browser has a session ID ────────────────
    import uuid as _uuid

    @app.before_request
    def ensure_browser_session():
        """Assign a stable browser_id to every visitor via a signed session cookie."""
        if 'browser_id' not in session:
            session['browser_id'] = _uuid.uuid4().hex
            session.permanent = True  # use PERMANENT_SESSION_LIFETIME if set

    # ── Blueprints ────────────────────────────────────────────────────────────
    app.register_blueprint(main_bp)

    # ── SocketIO ─────────────────────────────────────────────────────────────
    socketio.init_app(
        app,
        # Allow all origins when CORS_ALLOWED_ORIGINS is not configured (dev default).
        # Set CORS_ALLOWED_ORIGINS env var to a comma-separated list for production.
        cors_allowed_origins=CORS_ALLOWED_ORIGINS or '*',
        # manage_session=True (default) is required so Flask session is accessible
        # inside SocketIO event handlers (e.g. session.get('browser_id')).
    )
    register_socket_events(socketio)

    log.info('MCP Inspector application ready.')
    return app
