"""
Poll app load test
==================
Simulates concurrent users casting votes while SSE streams stay open.

Three phases:
  1. Discover  — GET / to find option IDs from the live app
  2. Connect   — open N SSE streams (watchers)
  3. Vote      — ramp up concurrent voters casting random votes

Metrics reported per tier:
  - SSE connect time (TTFE)
  - Vote POST latency (RTT)
  - SSE broadcast latency (time from vote POST to event arriving)
  - Votes/sec throughput
  - Any failures

Run the poll server first:
    uv run python vote.py

Then:
    uv run python load_test_poll.py
"""

import asyncio
import time
import statistics
import random
import re
import aiohttp

BASE = "http://0.0.0.0:8000"

CONNECT_TIMEOUT   = 20.0
BROADCAST_TIMEOUT = 8.0
CONNECT_STAGGER   = 0.005   # 5ms between SSE client spawns

# Ramp tiers: (n_watchers, n_voters, votes_per_voter, delay_between_votes)
TIERS = [
    (  5,  5,  10, 0.1),
    ( 10, 10,  20, 0.05),
    ( 25, 20,  20, 0.05),
    ( 50, 30,  20, 0.05),
    (100, 50,  15, 0.05),
    (200, 80,  10, 0.05),
    (300, 80,  10, 0.05),
    (400, 80,  10, 0.05),
    (500, 80,  10, 0.05),
]

# ── Helpers ───────────────────────────────────────────────────────────────────

def ms(s):  return f"{s*1000:.1f}ms"
def p95(lst):
    if not lst: return 0
    return sorted(lst)[max(0, int(len(lst) * 0.95) - 1)]

def parse_sse_events(buf: str) -> tuple[list[str], str]:
    parts    = buf.split("\n\n")
    complete = [e for e in parts[:-1] if e.strip() and not e.strip().startswith(":")]
    return complete, parts[-1]

# ── Discover option IDs from the live app ─────────────────────────────────────

async def discover_options(session: aiohttp.ClientSession) -> list[int]:
    """Fetch the landing page and extract option IDs from data-on:click attrs."""
    async with session.get(BASE, timeout=aiohttp.ClientTimeout(total=5)) as resp:
        html = await resp.text()
    # Extract option_id values from @post('/vote?option_id=N')
    ids = [int(m) for m in re.findall(r"option_id=(\d+)", html)]
    return sorted(set(ids))

# ── SSE watcher ───────────────────────────────────────────────────────────────

class Watcher:
    def __init__(self, wid: int, session: aiohttp.ClientSession, delay: float):
        self.id             = wid
        self.session        = session
        self.delay          = delay
        self.event_count    = 0
        self.first_event_at = None
        self.error          = None
        self._task          = None
        self._waiters: list[tuple[int, asyncio.Event]] = []

    def wait_for_count(self, n: int) -> asyncio.Event:
        ev = asyncio.Event()
        if self.event_count >= n:
            ev.set()
        else:
            self._waiters.append((n, ev))
        return ev

    def _on_event(self):
        self.event_count += 1
        done = [(n, ev) for n, ev in self._waiters if self.event_count >= n]
        for item in done:
            item[1].set()
            self._waiters.remove(item)

    async def run(self, ready: asyncio.Event):
        if self.delay:
            await asyncio.sleep(self.delay)
        try:
            async with self.session.get(
                f"{BASE}/stream",
                headers={"Accept": "text/event-stream",
                         "Accept-Encoding": "gzip, deflate"},
                timeout=aiohttp.ClientTimeout(total=300),
            ) as resp:
                if resp.status != 200:
                    self.error = f"HTTP {resp.status}"
                    ready.set()
                    return
                buf = ""
                async for chunk in resp.content.iter_any():
                    buf += chunk.decode(errors="replace")
                    events, buf = parse_sse_events(buf)
                    for _ in events:
                        if self.first_event_at is None:
                            self.first_event_at = time.perf_counter()
                            ready.set()
                        self._on_event()
        except asyncio.CancelledError:
            pass
        except Exception as e:
            self.error = str(e)
            ready.set()

    def start(self, ready: asyncio.Event):
        self._task = asyncio.create_task(self.run(ready))

    def stop(self):
        if self._task:
            self._task.cancel()

# ── Voter ─────────────────────────────────────────────────────────────────────

async def cast_vote(
    session: aiohttp.ClientSession,
    option_id: int,
) -> float | None:
    """POST /vote and return RTT in seconds, or None on error."""
    t0 = time.perf_counter()
    try:
        async with session.post(
            f"{BASE}/vote?option_id={option_id}",
            headers={"Content-Type": "application/json",
                     "Datastar-Request": "true"},
            json={"datastar": {}},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            await resp.read()
            if resp.status != 200:
                return None
            return time.perf_counter() - t0
    except Exception:
        return None

async def voter_task(
    session: aiohttp.ClientSession,
    option_ids: list[int],
    n_votes: int,
    delay: float,
    results: list,
):
    """Cast n_votes random votes, recording RTT for each."""
    for _ in range(n_votes):
        opt = random.choice(option_ids)
        rtt = await cast_vote(session, opt)
        results.append(rtt)
        if delay:
            await asyncio.sleep(delay)

# ── Tier runner ───────────────────────────────────────────────────────────────

async def run_tier(
    session: aiohttp.ClientSession,
    option_ids: list[int],
    n_watchers: int,
    n_voters: int,
    votes_per_voter: int,
    vote_delay: float,
) -> dict:

    # Phase 1: connect watchers
    watchers     = []
    ready_events = []
    t0           = time.perf_counter()

    for i in range(n_watchers):
        ready = asyncio.Event()
        ready_events.append(ready)
        w = Watcher(i, session, delay=i * CONNECT_STAGGER)
        w.start(ready)
        watchers.append(w)

    try:
        await asyncio.wait_for(
            asyncio.gather(*[e.wait() for e in ready_events]),
            timeout=CONNECT_TIMEOUT,
        )
    except asyncio.TimeoutError:
        pass

    connected = [w for w in watchers if w.first_event_at]
    ttfe = sorted([
        w.first_event_at - (t0 + w.delay)
        for w in connected
        if w.first_event_at > (t0 + w.delay)
    ])

    # Phase 2: fire votes + measure broadcast latency
    vote_rtts       = []
    bc_latencies    = []
    total_votes     = n_voters * votes_per_voter
    t_vote_start    = time.perf_counter()

    # Register broadcast probes — each watcher should receive
    # at least (current_count + 1) events after voting starts
    base_counts = [w.event_count for w in connected]

    # Run all voters concurrently
    voter_results: list[float | None] = []
    voter_tasks = [
        asyncio.create_task(
            voter_task(session, option_ids, votes_per_voter,
                       vote_delay, voter_results)
        )
        for _ in range(n_voters)
    ]
    await asyncio.gather(*voter_tasks)

    t_vote_end    = time.perf_counter()
    vote_duration = t_vote_end - t_vote_start
    successful    = [r for r in voter_results if r is not None]
    failed_votes  = len(voter_results) - len(successful)

    # Wait for all watchers to receive at least one more event than they
    # started with (confirms broadcasts reached them)
    bc_probes = [
        w.wait_for_count(base_counts[i] + 1)
        for i, w in enumerate(connected)
    ]
    t_bc_start = time.perf_counter()
    try:
        await asyncio.wait_for(
            asyncio.gather(*[p.wait() for p in bc_probes]),
            timeout=BROADCAST_TIMEOUT,
        )
        bc_latencies.append(time.perf_counter() - t_bc_start)
    except asyncio.TimeoutError:
        pass

    bc_received = sum(1 for p in bc_probes if p.is_set())

    # Small settle time then teardown
    await asyncio.sleep(0.3)
    for w in watchers:
        w.stop()
    await asyncio.sleep(0.2)

    return dict(
        n_watchers    = n_watchers,
        n_voters      = n_voters,
        votes_per_voter = votes_per_voter,
        connected     = len(connected),
        ttfe          = ttfe,
        total_votes   = total_votes,
        successful    = len(successful),
        failed_votes  = failed_votes,
        vote_rtts     = successful,
        vote_duration = vote_duration,
        bc_received   = bc_received,
        bc_latency    = bc_latencies[0] if bc_latencies else None,
    )

# ── Reporting ─────────────────────────────────────────────────────────────────

def report(r: dict):
    vps = r['successful'] / r['vote_duration'] if r['vote_duration'] else 0
    ok  = (r['connected'] == r['n_watchers'] and
           r['failed_votes'] == 0 and
           r['bc_received'] == r['connected'])
    icon = "✓" if ok else "✗"
    rtt = r['vote_rtts']
    ttfe = r['ttfe']

    print(f"\n  {icon} {r['n_watchers']} watchers  "
          f"{r['n_voters']} voters × {r['votes_per_voter']} votes")
    print(f"    Watchers  : {r['connected']}/{r['n_watchers']}"
          + (f"  [errors: {r['n_watchers']-r['connected']}]"
             if r['connected'] < r['n_watchers'] else ""))
    if ttfe:
        print(f"    TTFE      : min={ms(min(ttfe))}  "
              f"avg={ms(statistics.mean(ttfe))}  "
              f"p95={ms(p95(ttfe))}  max={ms(max(ttfe))}")
    print(f"    Votes     : {r['successful']}/{r['total_votes']} ok"
          + (f"  [{r['failed_votes']} failed]" if r['failed_votes'] else "")
          + f"  ({vps:.1f} votes/sec)")
    if rtt:
        print(f"    Vote RTT  : avg={ms(statistics.mean(rtt))}  "
              f"p95={ms(p95(rtt))}  max={ms(max(rtt))}")
    if r['bc_latency'] is not None:
        print(f"    Broadcast : {ms(r['bc_latency'])} "
              f"({r['bc_received']}/{r['connected']} watchers received)")
    else:
        print(f"    Broadcast : TIMEOUT "
              f"({r['bc_received']}/{r['connected']} watchers received)")

# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    print(f"\n{'='*58}")
    print(f"  Poll app load test  —  {BASE}")
    print(f"{'='*58}")

    # Discover option IDs from the live app
    connector = aiohttp.TCPConnector(limit=10)
    async with aiohttp.ClientSession(connector=connector) as s:
        option_ids = await discover_options(s)

    if not option_ids:
        print("  ✗ No option IDs found — is the server running?")
        return

    print(f"\n  Found {len(option_ids)} options: {option_ids}")

    results = []
    for n_watchers, n_voters, vpv, delay in TIERS:
        print(f"\n── {n_watchers} watchers / {n_voters} voters ──", flush=True)
        connector = aiohttp.TCPConnector(
            limit=n_watchers + n_voters + 20,
            force_close=True,
        )
        async with aiohttp.ClientSession(connector=connector) as session:
            try:
                r = await run_tier(
                    session, option_ids,
                    n_watchers, n_voters, vpv, delay,
                )
                results.append(r)
                report(r)

                # Stop if things are falling apart
                if r['connected'] < n_watchers * 0.9:
                    print("\n  ⚠  >10% watcher connect failures — stopping")
                    break
                if r['failed_votes'] > r['total_votes'] * 0.1:
                    print("\n  ⚠  >10% vote failures — stopping")
                    break

            except Exception as e:
                import traceback
                print(f"  ✗ tier crashed: {e}")
                traceback.print_exc()
                break

    # Summary
    print(f"\n{'='*58}")
    print(f"  SUMMARY")
    print(f"{'='*58}")
    print(f"  {'watchers':>9}  {'voters':>7}  {'vps':>8}  "
          f"{'rtt_p95':>9}  {'bc_lat':>9}  {'ok':>6}")
    print(f"  {'-'*9}  {'-'*7}  {'-'*8}  {'-'*9}  {'-'*9}  {'-'*6}")
    for r in results:
        vps  = r['successful'] / r['vote_duration'] if r['vote_duration'] else 0
        rtt  = r['vote_rtts']
        ok   = r['successful'] == r['total_votes'] and r['failed_votes'] == 0
        bc   = ms(r['bc_latency']) if r['bc_latency'] else 'TIMEOUT'
        print(f"  {r['n_watchers']:>9}  "
              f"{r['n_voters']:>7}  "
              f"{vps:>7.1f}/s  "
              f"{ms(p95(rtt)) if rtt else 'n/a':>9}  "
              f"{bc:>9}  "
              f"{'✓' if ok else '✗':>6}")
    print()


if __name__ == "__main__":
    asyncio.run(main())
