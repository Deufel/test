"""
md_web.db — SQLite via APSW (sync connection, async pub/sub)
=============================================================
Philosophy
----------
SQLite with WAL mode delivers ~70,000 reads/sec and ~3,600 writes/sec.
For the vast majority of web applications this is never the bottleneck.
Async DB wrappers add complexity without benefit at these speeds.

This module uses a plain sync APSW connection for all DB operations.
The only async concern is signalling the SSE layer when data changes —
handled by a single asyncio.Event via call_soon_threadsafe.

Public surface
--------------
    create_db(path)          → apsw.Connection, WAL + best practices
    migrate(conn, sql)       → idempotent schema, sync
    query(conn, sql, ...)    → list of tuples, sync
    write(conn, fn, *args)   → run fn(conn, *args) in a transaction, sync
    create_db_relay(conn, loop) → DbRelay with subscribe() + broadcaster

Pattern
-------
    # startup (sync — call from on_init via loop.run_until_complete)
    db    = create_db("app.db")
    relay = create_db_relay(db, loop)
    migrate(db, SCHEMA)

    # write handler
    @app.post("/vote")
    async def vote(req):
        write(db, lambda c: c.execute("INSERT INTO votes ..."))
        # update_hook fires, relay renders once, broadcasts to all streams
        async def _stream():
            yield patch_signals({})
        return _stream()

    # stream handler
    @app.get("/stream")
    async def stream(req):
        async def _stream():
            yield relay.broadcaster.current()  # initial state
            async for event_str in relay.broadcaster.subscribe():
                yield event_str
        return _stream()

Requires
--------
    uv add apsw
"""

from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any, Callable

import apsw
import apsw.bestpractice
import apsw.ext

log = logging.getLogger(__name__)

# Apply recommended settings once at import time:
# WAL, foreign keys, synchronous=NORMAL, recursive triggers, mmap, cache
apsw.bestpractice.apply(apsw.bestpractice.recommended)
apsw.ext.log_sqlite()


# ── Connection ────────────────────────────────────────────────────────────────

def create_db(path: str) -> apsw.Connection:
    """Open (or create) a SQLite connection with best practices applied.

    WAL mode, foreign keys, synchronous=NORMAL — all set by bestpractice.
    Returns a plain sync apsw.Connection. Fast, simple, no async wrapper.

    Call from on_init::

        def startup(loop):
            global db, relay
            db    = create_db("app.db")
            relay = create_db_relay(db, loop)
            migrate(db, SCHEMA)

        app = create_app(on_init=startup)
    """
    conn = apsw.Connection(path)
    conn.pragma("journal_mode", "wal")
    log.debug("db opened: %s", path)
    return conn


# ── Schema ────────────────────────────────────────────────────────────────────

def migrate(conn: apsw.Connection, schema_sql: str) -> None:
    """Apply schema idempotently. Use CREATE TABLE IF NOT EXISTS.

    Safe to call on every startup::

        migrate(db, '''
            CREATE TABLE IF NOT EXISTS messages (
                id   INTEGER PRIMARY KEY,
                text TEXT NOT NULL,
                ts   REAL NOT NULL DEFAULT (unixepoch('now','subsec'))
            );
        ''')
    """
    with conn:
        conn.execute(schema_sql)
    log.debug("migration applied")


# ── Query helpers ─────────────────────────────────────────────────────────────

def query(
    conn: apsw.Connection,
    sql: str,
    bindings: tuple = (),
    *,
    limit: int = 1000,
) -> list[tuple]:
    """Execute a SELECT, return plain tuples, capped at limit rows.

    Sync — runs directly in the calling thread. At SQLite WAL speeds
    (~70k reads/sec) this is microseconds, not a blocking concern.

        rows = query(db, "SELECT * FROM messages ORDER BY id DESC LIMIT 50")
        for row in rows:
            row[0]  # id
            row[1]  # text
    """
    rows = []
    for row in conn.execute(sql, bindings):
        rows.append(row)
        if len(rows) >= limit:
            log.debug("query: row limit %d hit", limit)
            break
    return rows


def write(
    conn: apsw.Connection,
    fn: Callable,
    *args: Any,
) -> Any:
    """Run fn(conn, *args) inside a transaction.

    The update_hook fires after commit — no manual relay.publish() needed.

        write(db, lambda c: c.execute(
            "INSERT INTO messages(text) VALUES(?)", (text,)
        ))
    """
    with conn:
        return fn(conn, *args)


# ── Broadcaster ───────────────────────────────────────────────────────────────

class Broadcaster:
    """
    Pre-renders once per DB write, fans out the same string to all streams.

    On every DB write:
      1. render_fn() called ONCE  → SSE event string
      2. string cached
      3. asyncio.Event set        → all N subscribers wake, read cache

    Per-subscriber work per update:
      - one attribute read (_cached)
      - yield the string (framework handles brotli per-client)

    Total renders per write: 1, regardless of N connected clients.
    """

    def __init__(self, render_fn: Callable[[], str]):
        self._render_fn = render_fn
        self._cached: str | None = None
        self._event = asyncio.Event()

    def _notify(self) -> None:
        """Called from update_hook (sync thread) via call_soon_threadsafe."""
        try:
            self._cached = self._render_fn()
        except Exception:
            log.exception("Broadcaster: render_fn raised")
            self._cached = None
        # Broadcast-future pattern: replace event, set old one
        old          = self._event
        self._event  = asyncio.Event()
        old.set()

    def current(self) -> str | None:
        """Return the last rendered SSE string (for initial client state)."""
        return self._cached

    async def subscribe(self):
        """Yield pre-rendered SSE strings on every DB write.

        All subscribers receive the same cached string object — zero
        per-subscriber rendering::

            async for event_str in relay.broadcaster.subscribe():
                if event_str:
                    yield event_str
        """
        try:
            while True:
                await self._event.wait()
                yield self._cached
        except (asyncio.CancelledError, GeneratorExit):
            pass


# ── Relay ─────────────────────────────────────────────────────────────────────

class DbRelay:
    """
    Connects SQLite's sync update_hook to asyncio SSE streams.

    The update_hook fires synchronously in SQLite's thread after every
    INSERT/UPDATE/DELETE. We hand off to the event loop via
    call_soon_threadsafe — one call that renders once and wakes all streams.
    """

    def __init__(
        self,
        conn: apsw.Connection,
        loop: asyncio.AbstractEventLoop,
        render_fn: Callable[[], str] | None = None,
    ):
        self._conn       = conn
        self._loop       = loop
        self.broadcaster = Broadcaster(render_fn or (lambda: ""))
        conn.set_update_hook(self._on_db_write)
        log.debug("DbRelay: update_hook installed")

    def _on_db_write(
        self,
        op_type: int,
        db_name: str,
        table_name: str,
        rowid: int,
    ) -> None:
        """Fires synchronously after every DB write. Must be fast."""
        self._loop.call_soon_threadsafe(self.broadcaster._notify)

    def set_render_fn(self, render_fn: Callable[[], str]) -> None:
        """Set or replace the render function after construction."""
        self.broadcaster._render_fn = render_fn

    def close(self) -> None:
        """Remove the update_hook."""
        self._conn.set_update_hook(None)
        log.debug("DbRelay: update_hook removed")


def create_db_relay(
    conn: apsw.Connection,
    loop: asyncio.AbstractEventLoop,
    render_fn: Callable[[], str] | None = None,
) -> DbRelay:
    """Create a DbRelay wired to conn's update_hook.

    render_fn is a sync callable that returns the SSE event string.
    It is called once per DB write before any subscriber wakes up.
    Pass it here or set it later with relay.set_render_fn().

    Call from on_init — the loop argument is the one passed by Granian::

        def startup(loop):
            global db, relay
            db    = create_db("app.db")
            relay = create_db_relay(db, loop)
            migrate(db, SCHEMA)
            relay.set_render_fn(lambda: patch_elements(render_app(db)))
    """
    return DbRelay(conn, loop, render_fn)
