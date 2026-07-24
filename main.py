import os

from backend.app import create_app, socketio

app = create_app()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("FLASK_DEBUG", "true").lower() == "true"
    print(f"Starting MCP Inspector on http://localhost:{port}")
    # use_reloader=False is required for Flask-SocketIO: the Werkzeug reloader
    # forks a child process which breaks the background asyncio threads used
    # by MCPConnectionManager and ServerRegistry, causing a hang on reload.
    socketio.run(app, debug=debug, use_reloader=False, host="0.0.0.0", port=port)
