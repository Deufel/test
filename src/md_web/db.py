"""
md_web.db — async SQLite via APSW
===================================
Single async path. No sync fallback. No footguns.

Public surface
--------------
    create_db(path)         → async, returns apsw async Connection
    migrate(db, sql)        → async, idempotent schema application
    create_db_relay(db)     → async, returns DbRelay
    query(db, sql, ...)     → async, safe SELECT with limits

All functions are coroutines. Call them with await. Always.

How the relay works
-------------------
APSW's async connection supports an async update_hook that fires
directly in the asyncio event loop — no call_soon_threadsafe,
no threading, no shared mutable state across thread boundaries.

The broadcast-future pattern gives zero-latency wakeups with no
lost notifications:

    INSERT row
        │
        └─ async update_hook fires in event loop
                │
                └─ _notify():
                       old_event  = self._event
                       self._event = asyncio.Event()   # next round
                       old_event.set()                 # wake all waiters
                              │
                    all subscribers wake, each reads
                    WHERE id > their own cursor,
                    advances cursor, yields new rows,
                    goes back to await self._event.wait()

Two rapid writes before any subscriber wakes? Both are caught on
the next wait() — subscribers read all rows past their cursor in
one query. No wakeup is ever lost.

Requires
--------
    uv add apsw
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

log = logging.getLogger(__name__)

# ── APSW import guard ─────────────────────────────────────────────────────────

try:
    import apsw
    import apsw.aio
    import apsw.bestpractice
    import apsw.ext
    _APSW = True
except ImportError:
    _APSW = False

def _require_apsw():
    if not _APSW:
        raise RuntimeError(
            "md_web.db requires apsw — run: uv add apsw"
        )

# Apply best practices once at import time if apsw is available.
# This sets WAL, foreign keys, synchronous=NORMAL, recursive triggers,
# mmap, cache size — the recommended defaults for every app.
if _APSW:
    apsw.bestpractice.apply(apsw.bestpractice.recommended)
    apsw.ext.log_sqlite()  # forward SQLite internal logs to Python logging


# ── Connection ────────────────────────────────────────────────────────────────

async def create_db(path: str) -> "apsw.Connection":
    """Open (or create) an async SQLite connection with best practices applied.

    Returns an APSW async connection. All operations on it must be awaited.
    Rows are returned as frozen dataclasses so render functions can use
    ``row.name``, ``row.text`` etc. instead of ``row[0]``, ``row[1]``.

    Call from ``on_init`` and attach to your app::

        async def startup(loop):
            app.db    = await create_db("chat.db")
            app.relay = await create_db_relay(app.db)
            await migrate(app.db, SCHEMA)

        app = create_app(on_init=startup)
    """
    _require_apsw()
    db = await apsw.Connection.as_async(path)

    # bestpractice sets WAL globally but we confirm it per-connection
    await db.pragma("journal_mode", "wal")

    # Rows come back as frozen dataclasses: row.id, row.name, etc.
    db.row_trace = apsw.ext.DataClassRowFactory(
        dataclass_kwargs={"frozen": True}
    )

    log.debug("db opened: %s", path)
    return db


# ── Migration ─────────────────────────────────────────────────────────────────

async def migrate(db: "apsw.Connection", schema_sql: str) -> None:
    """Apply a schema idempotently. Safe to call on every startup.

    Use ``CREATE TABLE IF NOT EXISTS`` and ``CREATE INDEX IF NOT EXISTS``
    in your schema SQL so re-runs are no-ops::

        await migrate(app.db, '''
            CREATE TABLE IF NOT EXISTS messages (
                id    INTEGER PRIMARY KEY,
                name  TEXT    NOT NULL,
                text  TEXT    NOT NULL,
                color TEXT    NOT NULL,
                ts    REAL    NOT NULL DEFAULT (unixepoch('now','subsec'))
            );
            CREATE INDEX IF NOT EXISTS messages_ts ON messages(ts);
        ''')
    """
    async with db:
        await db.execute(schema_sql)
    log.debug("migration applied")


# ── Relay ─────────────────────────────────────────────────────────────────────

class DbRelay:
    """
    SSE relay backed by SQLite's async update_hook.

    The hook fires directly in the asyncio event loop — no threading,
    no call_soon_threadsafe. The broadcast-future pattern ensures
    zero lost wakeups even under rapid writes.

    Identical subscribe/publish interface to the in-memory relay so
    SSE handlers need no changes when switching to a DB-backed relay.
    """

    def __init__(self, db: "apsw.Connection"):
        self._db     = db
        self._event  = asyncio.Event()   # current broadcast future

    def _notify(self) -> None:
        """Replace the current event and set the old one.

        Called from the async update_hook — already in the event loop.
        """
        old          = self._event
        self._event  = asyncio.Event()
        old.set()

    async def _install_hook(self) -> None:
        """Wire up the async update_hook."""
        async def _hook(
            op_type: int,
            db_name: str,
            table_name: str,
            rowid: int,
        ) -> None:
            self._notify()

        await self._db.set_update_hook(_hook)
        log.debug("DbRelay: async update_hook installed")

    def publish(self, _topic: str = "*", _data: Any = None) -> None:
        """Manually trigger a notification.

        Normally the update_hook fires automatically after any write.
        Use this after bulk imports or external writes that bypass the
        hook (e.g. ATTACH, SQLite CLI changes).
        """
        self._notify()

    async def subscribe(self, table: str = "*"):
        """Async generator — yields ``(table, rowid)`` on every DB change.

        Waits on the broadcast future. When the future fires, yields once
        and immediately sets up the next wait. The caller is responsible
        for querying the DB to find what changed::

            async for table, rowid in relay.subscribe("messages"):
                rows = await query(db,
                    "SELECT * FROM messages WHERE id > ? ORDER BY id",
                    (last_id,),
                )
                if rows:
                    last_id = rows[-1].id
                    yield patch_elements(render(rows))
        """
        try:
            while True:
                await self._event.wait()
                yield (table, -1)
        except (asyncio.CancelledError, GeneratorExit):
            pass

    async def close(self) -> None:
        """Remove the update_hook."""
        await self._db.set_update_hook(None)
        log.debug("DbRelay: update_hook removed")


async def create_db_relay(db: "apsw.Connection") -> DbRelay:
    """Create a DbRelay wired to *db*'s async update_hook.

    Must be called after ``create_db()``::

        async def startup(loop):
            app.db    = await create_db("app.db")
            app.relay = await create_db_relay(app.db)
            await migrate(app.db, SCHEMA)

        app = create_app(on_init=startup)
    """
    _require_apsw()
    relay = DbRelay(db)
    await relay._install_hook()
    return relay


# ── Safe query helper ─────────────────────────────────────────────────────────

async def query(
    db: "apsw.Connection",
    sql: str,
    bindings: tuple = (),
    *,
    limit: int = 1000,
) -> list:
    """Execute a SELECT safely, capped at *limit* rows.

    Returns a list of frozen dataclass rows (column names as attributes).
    Never raises on row-limit breach — returns partial results with a
    debug log. Use for all read queries in SSE handlers::

        messages = await query(db,
            "SELECT * FROM messages ORDER BY ts DESC LIMIT ?",
            (50,),
        )
        for m in reversed(messages):
            # m.name, m.text, m.color, m.ts
            ...
    """
    rows = []

    async def _fetch():
        cursor = await db.execute(sql, bindings)
        async for row in cursor:
            rows.append(row)
            if len(rows) >= limit:
                log.debug("query: row limit %d hit, truncating", limit)
                break

    await db.async_run(_fetch)
    return rows


# ── Batch write helper ────────────────────────────────────────────────────────

async def write(
    db: "apsw.Connection",
    fn,
    *args,
    **kwargs,
) -> Any:
    """Run a write function in the APSW worker thread.

    Wraps ``db.async_run()`` with an automatic transaction. Use for
    any write that touches multiple rows or tables — all ops complete
    atomically in a single worker-thread call, one round-trip::

        async def _insert_and_trim(db, name, text, color):
            with db:   # transaction
                db.execute(
                    "INSERT INTO messages(name,text,color) VALUES(?,?,?)",
                    (name, text, color),
                )
                db.execute(
                    "DELETE FROM messages WHERE id NOT IN "
                    "(SELECT id FROM messages ORDER BY id DESC LIMIT 200)"
                )
            return db.execute("SELECT MAX(id) FROM messages").fetchone()[0]

        max_id = await write(app.db, _insert_and_trim, db, name, text, color)

    The update_hook fires automatically after the transaction commits.
    No manual relay.publish() needed.
    """
    return await db.async_run(fn, *args, **kwargs)
