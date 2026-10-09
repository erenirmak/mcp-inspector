"""
Central configuration — all tuneable values are read from environment variables
so the app can be deployed without code changes.

Precedence: environment variable > .env file > hardcoded default.
"""

import ipaddress
import logging
import os
import secrets
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as _FutureTimeout

log = logging.getLogger(__name__)

# Bounded-timeout DNS resolver. getaddrinfo is a blocking syscall that can hang
# for tens of seconds on slow/blackholed DNS; running it in a small thread pool
# lets us impose a hard timeout without freezing the request thread.
_resolver = ThreadPoolExecutor(max_workers=4, thread_name_prefix="dns-resolver")

# ── Secret key ──────────────────────────────────────────────────────────────
_raw_secret = os.environ.get("SECRET_KEY", "").strip()
if not _raw_secret:
    _raw_secret = secrets.token_hex(32)
    log.warning(
        "SECRET_KEY is not set — a random key was generated. "
        "Sessions will be invalidated on every restart. "
        "Set the SECRET_KEY environment variable for production deployments."
    )
SECRET_KEY: str = _raw_secret

# ── Database ─────────────────────────────────────────────────────────────────
# Set DATABASE_URL=postgresql+psycopg2://user:pass@host/dbname for PostgreSQL.
# Defaults to a local SQLite file.
DATABASE_URL: str = os.environ.get("DATABASE_URL", "sqlite:///mcp_inspector.db").strip()

# ── Health checking ───────────────────────────────────────────────────────────
HEALTH_CHECK_INTERVAL: int = int(os.environ.get("HEALTH_CHECK_INTERVAL", "60"))
MAX_CONCURRENT_PROBES: int = max(1, int(os.environ.get("MAX_CONCURRENT_PROBES", "10")))

# ── MCP connection retries ────────────────────────────────────────────────────
MAX_RETRIES: int = max(1, int(os.environ.get("MCP_MAX_RETRIES", "3")))
RETRY_DELAY: float = max(0.5, float(os.environ.get("MCP_RETRY_DELAY", "2")))

# ── CORS ──────────────────────────────────────────────────────────────────────
# Comma-separated list of allowed origins, e.g. "http://localhost:3000,https://app.example.com"
# Leave empty to use same-origin only.
_raw_cors = os.environ.get("CORS_ALLOWED_ORIGINS", "").strip()
CORS_ALLOWED_ORIGINS: list[str] | str = (
    [o.strip() for o in _raw_cors.split(",") if o.strip()] if _raw_cors else []
)

# ── Request limits ────────────────────────────────────────────────────────────
MAX_CONTENT_LENGTH: int = int(os.environ.get("MAX_CONTENT_LENGTH_MB", "1")) * 1024 * 1024  # default 1 MB

# ── stdio connections ─────────────────────────────────────────────────────────
# stdio lets the server execute arbitrary local commands — disabled by default.
# Set STDIO_ENABLED=true to permit it (only in trusted/dev environments).
STDIO_ENABLED: bool = os.environ.get("STDIO_ENABLED", "false").lower() == "true"

# Comma-separated allowlist of permitted commands (basename only, e.g. "python,node,uvx").
# Empty string means *all* commands are allowed when STDIO_ENABLED=true.
_raw_cmds = os.environ.get("STDIO_ALLOWED_COMMANDS", "").strip()
STDIO_ALLOWED_COMMANDS: list[str] = [c.strip() for c in _raw_cmds.split(",") if c.strip()] if _raw_cmds else []

# Env-var keys that a client must never be allowed to inject
STDIO_BLOCKED_ENV_KEYS: frozenset[str] = frozenset(
    {
        "LD_PRELOAD",
        "LD_LIBRARY_PATH",
        "DYLD_INSERT_LIBRARIES",
        "DYLD_LIBRARY_PATH",
        "PYTHONPATH",
        "PATH",
        "CLASSPATH",
        "NODE_OPTIONS",
        "NODE_PATH",
    }
)

# ── Tool execution ────────────────────────────────────────────────────────────
TOOL_EXECUTION_TIMEOUT: int = int(os.environ.get("TOOL_EXECUTION_TIMEOUT", "30"))

# ── SSRF protection ───────────────────────────────────────────────────────────
# Set SSRF_PROTECTION=false to disable all URL validation (only in fully air-gapped envs).
SSRF_PROTECTION: bool = os.environ.get("SSRF_PROTECTION", "true").lower() != "false"

# Set ALLOW_LOCAL_SERVERS=false to also block localhost/127.x when SSRF protection is on.
# Defaults to true — local MCP servers are the primary use-case of this tool.
ALLOW_LOCAL_SERVERS: bool = os.environ.get("ALLOW_LOCAL_SERVERS", "true").lower() != "false"

# Comma-separated list of hostnames / domain suffixes explicitly trusted even if
# they resolve to a private IP.  Useful for homelabs and internal networks.
# Example: SSRF_TRUSTED_HOSTS=homelab.com,corp.internal,academia-mcp.homelab.com
_raw_trusted = os.environ.get("SSRF_TRUSTED_HOSTS", "").strip()
SSRF_TRUSTED_HOSTS: list[str] = (
    [h.strip().lower() for h in _raw_trusted.split(",") if h.strip()] if _raw_trusted else []
)

# Additional hostnames always blocked regardless of other settings
_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "metadata.google.internal",
        "metadata",
    }
)

# Networks blocked under SSRF protection.
# Loopback ranges (127/::1) are omitted when ALLOW_LOCAL_SERVERS=true.
_PRIVATE_NETS = [
    ipaddress.ip_network(cidr)
    for cidr in [
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",  # Carrier-grade NAT
        "169.254.0.0/16",  # Link-local / AWS IMDS
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",  # IPv6 ULA
        "fe80::/10",  # IPv6 link-local
    ]
    + ([] if ALLOW_LOCAL_SERVERS else ["127.0.0.0/8", "::1/128"])
]


def validate_server_url(url: str) -> str:
    """Parse and validate a server URL for SSRF risks.

    Returns the (unchanged) URL if it is acceptable.
    Raises ValueError with a user-safe message if not.
    """
    if not url:
        raise ValueError("URL must not be empty.")

    try:
        parsed = urllib.parse.urlparse(url)
    except Exception:
        raise ValueError("Malformed URL.")

    if parsed.scheme not in ("http", "https"):
        raise ValueError("Only http:// and https:// URLs are allowed.")

    hostname = (parsed.hostname or "").lower().strip()
    if not hostname:
        raise ValueError("URL must contain a valid hostname.")

    if not SSRF_PROTECTION:
        return url

    if hostname in _BLOCKED_HOSTNAMES:
        raise ValueError("URL hostname is not allowed.")

    # Check trusted-host allowlist — exact match or domain-suffix match.
    # e.g. trusting "homelab.com" also allows "academia-mcp.homelab.com".
    for trusted in SSRF_TRUSTED_HOSTS:
        if hostname == trusted or hostname.endswith("." + trusted):
            return url

    # Resolve and check every IP the hostname maps to.
    # getaddrinfo can block for 10-30s+ (or indefinitely) on slow/hanging DNS,
    # which would freeze the Flask request thread. Run it in a worker thread
    # with a hard timeout so registration never hangs.
    import socket as _sock

    def _resolve(host: str):
        return _sock.getaddrinfo(host, None)

    try:
        infos = _resolver.submit(_resolve, hostname).result(timeout=3)
    except _FutureTimeout:
        raise ValueError(f'DNS resolution for "{hostname}" timed out.')
    except _sock.gaierror:
        raise ValueError(f'Cannot resolve hostname "{hostname}".')

    for _, _, _, _, sockaddr in infos:
        raw_ip = sockaddr[0]
        try:
            addr = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        for net in _PRIVATE_NETS:
            if addr in net:
                raise ValueError("URL resolves to a private or reserved address and is not allowed.")

    return url
