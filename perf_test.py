"""
md-web chat — stress sweep
===========================
Runs escalating load tiers to find where latency degrades or clients drop.

Run the chat server first:
    uv run python demo_chat.py

Then:
    uv run python perf_test.py
"""

import asyncio
import gzip
import time
import statistics
import aiohttp

BASE = "http://0.0.0.0:8000"

CONNECT_TIMEOUT   = 30.0   # seconds to wait for ALL clients to get first event
BROADCAST_TIMEOUT = 10.0   # seconds to wait for a message to reach all clients
CONNECT_STAGGER   = 0.002  # seconds between each client's HTTP connect

# ── Stress tiers: (n_clients, n_messages, send_delay_s) ──────────────────────
TIERS = [
    (  20,  10, 0.30),
    (  50,  20, 0.20),
    ( 100,  20, 0.15),
    ( 200,  20, 0.10),
    ( 300,  15, 0.10),
    ( 500,  10, 0.10),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def ms(s):  return f"{s*1000:.1f}ms"
def kb(b):  return f"{b/1024:.1f}KB"

def parse_sse_events(buf: str) -> tuple[list[str], str]:
    """Split SSE buffer on double-newline boundaries, drop keepalives."""
    parts    = buf.split("\n\n")
    complete = parts[:-1]
    leftover = parts[-1]
    complete = [e for e in complete if e.strip() and not e.strip().startswith(":")]
    return complete, leftover

def try_decode_event(raw_bytes: bytes) -> str:
    """Try to decode an event chunk — handles both plain and gzip."""
    try:
        return raw_bytes.decode()
    except UnicodeDecodeError:
        pass
    try:
        return gzip.decompress(raw_bytes).decode()
    except Exception:
        return ""

# ── SSE client ────────────────────────────────────────────────────────────────

class SSEClient:
    def __init__(self, cid: int, session: aiohttp.ClientSession,
                 connect_delay: float = 0.0):
        self.id             = cid
        self.session        = session
        self.connect_delay  = connect_delay
        self.events         = []       # (receive_time, decoded_text)
        self.bytes_rx       = 0        # raw bytes off wire (before decompress)
        self.first_event_at = None
        self.errors         = []
        self.signals: dict[str, asyncio.Event] = {}
        self._task          = None
        # counter-based broadcast probe: fires when event_count reaches target
        self._count_target  = None
        self._count_event   = None

    def expect(self, marker: str) -> asyncio.Event:
        """Register a text marker to watch for in decoded event data."""
        ev = asyncio.Event()
        self.signals[marker] = ev
        return ev

    def expect_count(self, target: int) -> asyncio.Event:
        """Fire when total event count reaches target (works for binary events)."""
        ev = asyncio.Event()
        self._count_target = target
        self._count_event  = ev
        if len(self.events) >= target:
            ev.set()
        return ev

    async def run(self, ready: asyncio.Event):
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        try:
            async with self.session.get(
                f"{BASE}/stream",
                headers={
                    "Accept":          "text/event-stream",
                    # Accept gzip so broadcaster path is exercised.
                    # aiohttp decompresses gzip transparently giving plain text.
                    "Accept-Encoding": "gzip, deflate",
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                if resp.status != 200:
                    self.errors.append(f"HTTP {resp.status}")
                    ready.set()
                    return

                buf = ""
                async for chunk in resp.content.iter_any():
                    self.bytes_rx += len(chunk)
                    # aiohttp auto-decompresses gzip — chunk is plain text
                    text = chunk.decode(errors="replace")
                    buf += text
                    complete, buf = parse_sse_events(buf)

                    for raw in complete:
                        now = time.perf_counter()
                        self.events.append((now, raw))

                        if self.first_event_at is None:
                            self.first_event_at = now
                            ready.set()

                        # Text marker probes (works for str events)
                        for marker, ev in list(self.signals.items()):
                            if marker in raw:
                                ev.set()

                        # Count-based probe (works for any event type)
                        if (self._count_target is not None
                                and not self._count_event.is_set()
                                and len(self.events) >= self._count_target):
                            self._count_event.set()

        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.errors.append(str(e))
            ready.set()

    def start(self, ready: asyncio.Event):
        self._task = asyncio.create_task(self.run(ready))

    def stop(self):
        if self._task:
            self._task.cancel()


# ── Single tier ───────────────────────────────────────────────────────────────

async def run_tier(session, n_clients, n_messages, send_delay) -> dict:

    clients      = []
    ready_events = []
    t0           = time.perf_counter()

    for i in range(n_clients):
        ready = asyncio.Event()
        ready_events.append(ready)
        c = SSEClient(i, session, connect_delay=i * CONNECT_STAGGER)
        c.start(ready)
        clients.append(c)

    # Wait for all clients to receive their first event
    try:
        await asyncio.wait_for(
            asyncio.gather(*[e.wait() for e in ready_events]),
            timeout=CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        pass

    connected = [c for c in clients if c.first_event_at]
    failed    = [c for c in clients if c.errors]
    ttfe      = sorted([
        c.first_event_at - (t0 + c.connect_delay)
        for c in connected
        if c.first_event_at > (t0 + c.connect_delay)
    ])

    # Broadcast phase — use count-based probes since broadcaster sends
    # pre-compressed bytes that may not contain readable text markers
    send_rtts           = []
    broadcast_latencies = []
    broadcast_timeouts  = 0

    for i in range(n_messages):
        # Each client should have received (1 initial + i+1 broadcasts) by end
        target_count = 1 + i + 1
        live   = [c for c in connected]
        probes = [c.expect_count(target_count) for c in live]

        t_send = time.perf_counter()
        try:
            async with session.post(
                f"{BASE}/send",
                json={"datastar": {"msg": f"msg-{i}", "user": "stress"}},
                headers={"Content-Type": "application/json"},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as resp:
                await resp.read()
            send_rtts.append(time.perf_counter() - t_send)
        except Exception as e:
            print(f"    send error: {e}")

        try:
            await asyncio.wait_for(
                asyncio.gather(*[p.wait() for p in probes]),
                timeout=BROADCAST_TIMEOUT,
            )
            broadcast_latencies.append(time.perf_counter() - t_send)
        except asyncio.TimeoutError:
            broadcast_timeouts += 1
            received = sum(1 for p in probes if p.is_set())
            print(f"    broadcast timeout — {received}/{len(live)} "
                  f"received msg {i+1}")
            broadcast_latencies.append(BROADCAST_TIMEOUT)

        await asyncio.sleep(send_delay)

    await asyncio.sleep(0.3)
    for c in clients:
        c.stop()
    await asyncio.sleep(0.3)

    return {
        "n_clients":    n_clients,
        "n_messages":   n_messages,
        "send_delay":   send_delay,
        "connected":    len(connected),
        "failed":       len(failed),
        "ttfe":         ttfe,
        "send_rtts":    send_rtts,
        "latencies":    broadcast_latencies,
        "timeouts":     broadcast_timeouts,
        "bytes_rx":     [c.bytes_rx for c in connected],
        "event_counts": [len(c.events) for c in connected],
    }


def print_tier(r):
    n    = r["n_clients"]
    con  = r["connected"]
    ttfe = r["ttfe"]
    lat  = [l for l in r["latencies"] if l < BROADCAST_TIMEOUT]
    rtt  = r["send_rtts"]
    ok   = con == n and r["timeouts"] == 0

    print(f"\n  {'✓' if ok else '✗'} {n} clients  "
          f"({r['n_messages']} msgs @ {r['send_delay']*1000:.0f}ms intervals)")
    print(f"    Connect   : {con}/{n}"
          + (f"  [{r['failed']} errors]" if r["failed"] else ""))
    if ttfe:
        p95 = ttfe[max(0, int(len(ttfe)*0.95)-1)]
        print(f"    TTFE      : min={ms(ttfe[0])}  avg={ms(statistics.mean(ttfe))}"
              f"  p95={ms(p95)}  max={ms(ttfe[-1])}")
    if rtt:
        print(f"    Send RTT  : avg={ms(statistics.mean(rtt))}  max={ms(max(rtt))}")
    if lat:
        p95l = sorted(lat)[max(0, int(len(lat)*0.95)-1)]
        print(f"    Broadcast : avg={ms(statistics.mean(lat))}  "
              f"p95={ms(p95l)}  max={ms(max(lat))}"
              + (f"  [{r['timeouts']} timeouts]" if r["timeouts"] else ""))
    if r["bytes_rx"]:
        print(f"    Bytes/client : avg={kb(statistics.mean(r['bytes_rx']))}")
    if r["event_counts"]:
        ec  = r["event_counts"]
        exp = 1 + r["n_messages"]
        print(f"    Events/client: min={min(ec)}  avg={statistics.mean(ec):.1f}"
              f"  max={max(ec)}  (expected {exp})")


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'='*60}")
    print(f"  md-web chat — stress sweep")
    print(f"  {BASE}")
    print(f"  stagger={CONNECT_STAGGER*1000:.0f}ms  "
          f"connect_timeout={CONNECT_TIMEOUT}s  "
          f"broadcast_timeout={BROADCAST_TIMEOUT}s")
    print(f"{'='*60}")

    results = []
    for n_clients, n_messages, send_delay in TIERS:
        print(f"\n── Tier: {n_clients} clients ──", flush=True)
        connector = aiohttp.TCPConnector(limit=n_clients + 50, force_close=True)
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                r = await run_tier(session, n_clients, n_messages, send_delay)
                results.append(r)
                print_tier(r)

                if r["connected"] < n_clients * 0.9:
                    print(f"\n  ⚠  >10% connect failures — stopping")
                    break
                if r["timeouts"] > n_messages * 0.3:
                    print(f"\n  ⚠  >30% broadcast timeouts — stopping")
                    break

            except Exception as e:
                print(f"  ✗ tier crashed: {e}")
                import traceback; traceback.print_exc()
                break

    print(f"\n{'='*60}")
    print(f"  SWEEP SUMMARY")
    print(f"{'='*60}")
    print(f"  {'clients':>8}  {'ok':>7}  {'ttfe_p95':>10}  "
          f"{'bc_avg':>9}  {'bc_p95':>9}  {'timeouts':>8}")
    print(f"  {'-'*8}  {'-'*7}  {'-'*10}  {'-'*9}  {'-'*9}  {'-'*8}")
    for r in results:
        ttfe = r["ttfe"]
        lat  = [l for l in r["latencies"] if l < BROADCAST_TIMEOUT]
        p95t = ttfe[max(0, int(len(ttfe)*0.95)-1)] if ttfe else 0
        p95l = sorted(lat)[max(0, int(len(lat)*0.95)-1)] if lat else 0
        print(f"  {r['n_clients']:>8}  "
              f"{r['connected']:>4}/{r['n_clients']:<4}  "
              f"{ms(p95t):>10}  "
              f"{ms(statistics.mean(lat)) if lat else 'n/a':>9}  "
              f"{ms(p95l):>9}  "
              f"{r['timeouts']:>8}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
