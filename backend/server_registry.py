"""
MCP Server Registry — tracks known MCP servers, their online status, and cached tool metadata.

Persistence: every change is written to the configured database (PostgreSQL or SQLite).
Probing: servers are probed in parallel (bounded by MAX_CONCURRENT_PROBES).
Isolation: a health loop accumulation guard prevents overlapping sweeps.
Error handling: only the exception *type* is exposed, never raw exception strings that
  might contain credentials or internal addresses.
"""
import asyncio
import logging
import time
import threading
from dataclasses import dataclass, field, asdict

from .config import HEALTH_CHECK_INTERVAL, MAX_CONCURRENT_PROBES
from .db import get_db, ServerModel
from .mcp_manager import _fetch_server_data, is_url_busy
from .tool_search import tool_index

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# In-memory record
# ---------------------------------------------------------------------------

@dataclass
class ServerRecord:
    """A registered MCP server (in-memory view)."""
    id: str
    name: str
    url: str
    conn_type: str = 'http'
    online: bool = False
    last_checked: float = 0.0
    last_error: str = ''
    tools: list = field(default_factory=list)
    resources: list = field(default_factory=list)
    prompts: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class ServerRegistry:
    """Thread-safe, DB-backed registry of MCP servers with parallel health probing."""

    def __init__(self) -> None:
        self._servers: dict[str, ServerRecord] = {}
        self._lock = threading.Lock()
        self._health_running = False          # prevent overlapping health sweeps
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

        # Health loop starts immediately; load_from_db() is called from app.py
        # after init_db() completes, so the DB is guaranteed to be ready.
        asyncio.run_coroutine_threadsafe(self._health_loop(), self._loop)

    # ── Event loop ────────────────────────────────────────────────────────

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    # ── DB helpers ────────────────────────────────────────────────────────

    def load_from_db(self) -> None:
        """Populate in-memory cache from persisted DB rows on startup.

        Must be called *after* ``init_db()`` — i.e. from ``create_app()``.
        """
        return self._load_from_db()

    def _load_from_db(self) -> None:
        """Internal: load persisted rows into the in-memory cache."""
        try:
            with get_db() as db:
                rows = db.query(ServerModel).all()
                for row in rows:
                    rec = ServerRecord(
                        id=row.id,
                        name=row.name,
                        url=row.url,
                        conn_type=row.conn_type,
                        online=row.online,
                        last_checked=row.last_checked or 0.0,
                        last_error=row.last_error or '',
                        tools=row.tools or [],
                        resources=row.resources or [],
                        prompts=row.prompts or [],
                    )
                    self._servers[rec.id] = rec
            log.info('Loaded %d server(s) from database', len(self._servers))
        except RuntimeError as exc:
            # DB not yet initialised — this should not happen if load_from_db()
            # is called after init_db(), but guard defensively.
            log.warning('_load_from_db: DB not ready (%s) — starting with empty registry', exc)
        except Exception:
            log.exception('Failed to load servers from database — starting with empty registry')

    def _persist_server(self, rec: ServerRecord) -> None:
        """Write (upsert) a server record to the database."""
        try:
            with get_db() as db:
                row = db.get(ServerModel, rec.id)
                if row is None:
                    row = ServerModel(id=rec.id)
                    db.add(row)
                row.name = rec.name
                row.url = rec.url
                row.conn_type = rec.conn_type
                row.online = rec.online
                row.last_checked = rec.last_checked
                row.last_error = rec.last_error
                row.tools = rec.tools
                row.resources = rec.resources
                row.prompts = rec.prompts
        except Exception:
            log.exception('Failed to persist server %s to database', rec.id)

    def _delete_from_db(self, server_id: str) -> None:
        """Remove a server row from the database."""
        try:
            with get_db() as db:
                row = db.get(ServerModel, server_id)
                if row is not None:
                    db.delete(row)
        except Exception:
            log.exception('Failed to delete server %s from database', server_id)

    # ── Public API ────────────────────────────────────────────────────────

    def add_server(self, server_id: str, name: str, url: str,
                   conn_type: str = 'http') -> ServerRecord:
        rec = ServerRecord(id=server_id, name=name, url=url, conn_type=conn_type)
        with self._lock:
            self._servers[server_id] = rec
        self._persist_server(rec)
        asyncio.run_coroutine_threadsafe(self._probe(server_id), self._loop)
        return rec

    def remove_server(self, server_id: str) -> bool:
        with self._lock:
            removed = self._servers.pop(server_id, None) is not None
        if removed:
            self._delete_from_db(server_id)
        return removed

    def get_server(self, server_id: str) -> ServerRecord | None:
        with self._lock:
            return self._servers.get(server_id)

    def list_servers(self) -> list[dict]:
        with self._lock:
            return [s.to_dict() for s in self._servers.values()]

    def get_all_tools(self) -> list[dict]:
        """Return all tools from all online servers, annotated with server info."""
        results = []
        with self._lock:
            servers = list(self._servers.values())
        for srv in servers:
            for tool in srv.tools:
                entry = dict(tool) if isinstance(tool, dict) else {}
                entry['_server_id'] = srv.id
                entry['_server_name'] = srv.name
                entry['_server_url'] = srv.url
                entry['_server_online'] = srv.online
                results.append(entry)
        return results

    def refresh_server(self, server_id: str) -> None:
        """Trigger an immediate probe for one server."""
        asyncio.run_coroutine_threadsafe(self._probe(server_id), self._loop)

    def refresh_all(self) -> None:
        """Trigger an immediate probe of every server."""
        asyncio.run_coroutine_threadsafe(self._probe_all(), self._loop)

    # ── Probing internals ─────────────────────────────────────────────────

    async def _probe(self, server_id: str) -> None:
        """Connect to a server, cache its capabilities, update online status in DB."""
        with self._lock:
            rec = self._servers.get(server_id)
        if rec is None:
            return
        if is_url_busy(rec.url):
            return

        try:
            if rec.conn_type in ('http', 'sse'):
                data = await _fetch_server_data(rec.url)
                with self._lock:
                    rec.tools = data['tools']
                    rec.resources = data['resources']
                    rec.prompts = data['prompts']
                    rec.online = True
                    rec.last_checked = time.time()
                    rec.last_error = ''
                self._persist_server(rec)
                # Rebuild the full-text search index so searches reflect fresh data
                tool_index.rebuild(self.get_all_tools())
            # stdio: probing requires a local process — skip silently
        except RuntimeError:
            # _fetch_server_data raises RuntimeError when the probe slot is busy
            log.debug('Probe skipped for %s — server busy', server_id)
        except Exception as exc:
            # Sanitise: expose only the exception *type* so credentials/IPs don't leak
            safe_error = type(exc).__name__
            log.warning('Probe failed for %s: %s', server_id, exc)
            with self._lock:
                rec.online = False
                rec.last_checked = time.time()
                rec.last_error = safe_error
            self._persist_server(rec)

    async def _probe_all(self) -> None:
        """Probe all registered servers in parallel, bounded by MAX_CONCURRENT_PROBES."""
        with self._lock:
            ids = list(self._servers.keys())

        sem = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

        async def probe_with_sem(sid: str) -> None:
            async with sem:
                await self._probe(sid)

        await asyncio.gather(*[probe_with_sem(sid) for sid in ids])

    async def _health_loop(self) -> None:
        """Periodically probe all servers.  Skips a sweep if the previous one is still running."""
        while True:
            await asyncio.sleep(HEALTH_CHECK_INTERVAL)
            if self._health_running:
                log.debug('Health sweep still in progress — skipping this tick')
                continue
            self._health_running = True
            try:
                await self._probe_all()
            except Exception:
                log.exception('Unexpected error during health sweep')
            finally:
                self._health_running = False


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------
registry = ServerRegistry()
