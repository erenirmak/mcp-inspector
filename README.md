# MCP Inspector

A Flask-based tool to inspect MCP (Model Context Protocol) servers. It provides a modern web interface to connect to local (stdio) or remote (SSE) MCP servers and view their capabilities.

## Features

- **Connect** to local MCP servers via command execution (stdio).
- **Connect** to remote MCP servers via SSE.
- **Inspect** available Tools, Resources, and Prompts.
- **Real-time** logs and connection status.
- **Modern UI** built with Tailwind CSS.

## Prerequisites

- Python 3.10+
- [uv](https://github.com/astral-sh/uv) (recommended) or pip

## Installation

```bash
# Clone repository
cd mcp-inspector

# Install dependencies with uv
uv sync

# Or with pip
pip install -r requirements.txt
```

## Running the Inspector

```bash
# Using uv (recommended)
uv run python main.py

# Using python directly
python main.py
```

Open [http://localhost:5000](http://localhost:5000) in your browser.

## Project Structure

- `backend/`: Flask application and MCP client logic.
- `frontend/`: HTML templates and static assets.
- `main.py`: Entry point.

## Usage

1.  **Select Connection Type**: Choose "Local Process (stdio)" or "HTTP SSE (Remote)".
2.  **Configure**:
    - For `stdio`: Enter the command to run the MCP server (e.g., `python server.py`) and optional arguments.
    - For `sse`: Enter the URL of the MCP server (e.g., `http://localhost:8000/sse`).
3.  **Connect**: Click "Connect".
4.  **Inspect**: Browse Tools, Resources, and Prompts in the tabs.
