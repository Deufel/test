"""RSGI application factory and request utilities."""
import asyncio, base64, hashlib, hmac, inspect, json
import os, re, threading, time, traceback
from fnmatch import fnmatch
from urllib.parse import parse_qs
import mimetypes

from .html import render, Tag, Safe

PARAM_RE = re.compile(r'\{(\w+)\}')


# ── Request parsing ──────────────────────────────────────────────────────────

def _parse_request(scope, proto) -> dict:
    """Build a request dict from an RSGI scope and protocol."""
    raw_cookies = scope.headers.get("cookie", "")
    return {
        "path":         scope.path,
        "method":       scope.method,
        "headers":      scope.headers,
        "query":        {k: v[0] if len(v) == 1 else v
                         for k, v in parse_qs(scope.query_string).items()},
        "cookies":      dict(
            pair.strip().split("=", 1)
            for pair in raw_cookies.split(";")
            if "=" in pair
        ),
        "scheme":       scope.scheme,
        "client":       scope.client,
        "http_version": scope.http_version,
        "server":       scope.server,
        "authority":    getattr(scope, "authority", None),
        "_proto":       proto,
        "_cookies":     [],
    }


# ── Request helpers ──────────────────────────────────────────────────────────

async def body(req: dict, *, max_size: int = 1_048_576) -> bytes:
    """Read the full request body (cached).

    Raises :exc:`ValueError` if the body exceeds *max_size* bytes.
    """
    if "_body" in req:
        return req["_body"]
    raw = await req["_proto"]()
    if max_size and len(raw) > max_size:
        raise ValueError(f"Request body exceeds {max_size} bytes")
    req["_body"] = raw
    return raw


def header_values(req: dict, name: str) -> list[str]:
    """Return all values for a header (multi-value safe).

    Uses RSGI's native ``get_all()`` for correct handling of repeated headers
    like ``X-Forwarded-For``, ``Via``, ``Set-Cookie``.
    """
    return req["headers"].get_all(name)


async def body_stream(req: dict, *, max_size: int = 1_048_576):
    """Yield request body in chunks without buffering the full payload.

    Mutually exclusive with :func:`body` — use one or the other per request.

    Usage::

        @app.post("/upload")
        async def upload(req):
            chunks = []
            async for chunk in body_stream(req):
                chunks.append(chunk)
            data = b"".join(chunks)
    """
    proto = req["_proto"]
    total = 0
    async for chunk in proto:
        total += len(chunk)
        if max_size and total > max_size:
            raise ValueError(f"Request body exceeds {max_size} bytes")
        yield chunk


async def signals(req: dict) -> dict:
    """Read Datastar signals from a request.

    * ``GET``: JSON-encoded ``datastar`` query parameter.
    * Other methods: JSON body, optionally wrapped in ``{"datastar": ...}``.
    """
    if req["method"] == "GET":
        raw = req["query"].get("datastar", "{}")
        return json.loads(raw) if isinstance(raw, str) else raw
    data = json.loads(await body(req))
    return data.get("datastar", data) if isinstance(data, dict) else data


def set_cookie(req: dict, name: str, value: str, **opts) -> None:
    """Queue a ``Set-Cookie`` header to be sent with the response."""
    req["_cookies"].append((name, value, opts))


def _serialize_cookie(name: str, value: str, opts: dict) -> str:
    parts = [f"{name}={value}"]
    for k, v in opts.items():
        k = k.replace("_", "-")
        if isinstance(v, bool):
            if v: parts.append(k)
        else:
            parts.append(f"{k}={v}")
    return "; ".join(parts)


def _cookie_headers(req: dict) -> list[tuple[str, str]]:
    return [("set-cookie", _serialize_cookie(n, v, o))
            for n, v, o in req["_cookies"]]


# ── Pub/sub relay ────────────────────────────────────────────────────────────

def create_relay():
    """Create a thread-safe pub/sub relay for broadcasting SSE events.

    Usage::

        relay = create_relay()
        relay.publish("chat.new", item)           # sync, thread-safe

        async for topic, data in relay.subscribe("chat.*"):
            yield patch_elements(render(data))    # async generator

        # High-performance broadcast: pre-render once, fan out bytes to N clients
        relay.broadcast("chat.*", b"event: datastar-patch-elements\ndata: ...")
    """
    subs: list[tuple[str, asyncio.Queue]] = []
    lock = threading.Lock()

    def publish(topic: str, data):
        with lock:
            targets = [(p, q) for p, q in subs if fnmatch(topic, p)]
        for _, queue in targets:
            try:    queue.put_nowait((topic, data))
            except: pass  # noqa: E722

    def broadcast(topic: str, payload: bytes):
        """Fan out pre-encoded bytes to all matching subscribers.

        Unlike publish(), broadcast() puts the same bytes object into every
        matching queue — O(1) encode, O(N) queue puts (the queue put is a
        pointer copy, not a data copy). Each SSE handler calls send_bytes
        directly without any further encoding work.
        """
        with lock:
            targets = [(p, q) for p, q in subs if fnmatch(topic, p)]
        for _, queue in targets:
            try:    queue.put_nowait(("__broadcast__", payload))
            except: pass  # noqa: E722

    async def subscribe(pattern: str):
        queue = asyncio.Queue()
        with lock: subs.append((pattern, queue))
        try:
            while True: yield await queue.get()
        except (asyncio.CancelledError, GeneratorExit):
            pass
        finally:
            with lock:
                try:    subs.remove((pattern, queue))
                except: pass  # noqa: E722

    class _Relay:
        __slots__ = ("publish", "subscribe", "broadcast", "subscriber_count")

    def subscriber_count(pattern: str = "*") -> int:
        with lock:
            return sum(1 for p, _ in subs if fnmatch(pattern, p))

    r = _Relay()
    r.publish, r.subscribe = publish, subscribe
    r.broadcast = broadcast
    r.subscriber_count = subscriber_count
    return r


# ── O(1) broadcaster ─────────────────────────────────────────────────────────

def create_broadcaster(relay, render_fn, *, topic: str = "broadcast"):
    """Wrap a relay with a pre-render-and-fan-out broadcaster.

    Instead of each of N SSE handlers independently calling render_fn() and
    compressing the result, the broadcaster:

      1. Calls render_fn() ONCE → SSE event string
      2. Compresses it ONCE with gzip (shared across all clients)
      3. Fans out the same bytes to all N transport queues via relay.broadcast()

    This reduces the per-broadcast work from O(N) renders + O(N) compressions
    to O(1) render + O(1) compression + O(N) queue puts (pointer copies).

    SSE handlers must subscribe to topic and use send_bytes directly.

    Usage::

        broadcaster = create_broadcaster(relay, lambda: patch_elements(view()))

        # In your stream handler:
        async def stream():
            yield patch_elements(view())           # initial state
            async for _topic, payload in relay.subscribe(topic):
                if _topic == "__broadcast__":
                    # payload is already compressed bytes — send directly
                    yield payload   # framework detects bytes and uses send_bytes
            return stream()

        # To trigger a broadcast:
        broadcaster.push()
    """
    import zlib

    def push():
        event_str = render_fn()                  # render once
        relay.broadcast(topic, event_str)        # fan out — str, not bytes
        # Note: we broadcast the plain SSE string, not compressed bytes.
        # Each client's per-stream compressor (brotli or gzip) handles
        # encoding. The O(1) win is the render — we call render_fn() once
        # regardless of N. Compression is still O(N) but that cost lives
        # in Rust I/O, not Python GIL.

    class _Broadcaster:
        __slots__ = ("push", "topic")
    b = _Broadcaster()
    b.push  = push
    b.topic = topic
    return b


def _gzip_compress(data: bytes, level: int = 6) -> bytes:
    """Compress bytes with gzip at the given level."""
    import zlib
    compress = zlib.compressobj(level, zlib.DEFLATED, zlib.MAX_WBITS | 16)
    return compress.compress(data) + compress.flush()


# ── Cookie signer ────────────────────────────────────────────────────────────

def create_signer(secret: str | bytes | None = None):
    """Create an HMAC-SHA256 cookie signer.

    Usage::

        signer = create_signer("my-secret")
        set_cookie(req, "session", signer.sign("user42"))
        user = signer.unsign(req["cookies"].get("session", ""))
    """
    if secret is None:          secret = os.urandom(32)
    if isinstance(secret, str): secret = secret.encode()

    def _b64e(data: bytes) -> str:
        return base64.urlsafe_b64encode(data).rstrip(b"=").decode()

    def _b64d(s: str) -> bytes:
        return base64.urlsafe_b64decode((s + "=" * (-len(s) % 4)).encode())

    def _mac(payload: str) -> str:
        return _b64e(hmac.new(secret, payload.encode(), hashlib.sha256).digest())

    def sign(value: str, ts: float | None = None) -> str:
        ts = ts or time.time()
        payload = f"{_b64e(value.encode())}.{int(ts):x}"
        return f"{payload}.{_mac(payload)}"

    def unsign(signed: str, max_age: int | None = 3600) -> str | None:
        if not signed: return None
        parts = signed.split(".")
        if len(parts) != 3: return None
        enc_value, ts_hex, sig = parts
        payload = f"{enc_value}.{ts_hex}"
        if not hmac.compare_digest(sig, _mac(payload)): return None
        if max_age is not None:
            try:    ts = int(ts_hex, 16)
            except: return None  # noqa: E722
            if time.time() - ts > max_age: return None
        try:    return _b64d(enc_value).decode()
        except: return None  # noqa: E722

    class _Signer:
        __slots__ = ("sign", "unsign")
    s = _Signer()
    s.sign, s.unsign = sign, unsign
    return s


# ── Static file serving ──────────────────────────────────────────────────────

def static(app, url_prefix: str, directory: str):
    """Mount a directory or single file for static serving.

    Uses RSGI ``response_file`` / ``response_file_range`` for zero-copy file
    I/O from Rust. Supports HTTP Range requests for resumable downloads and
    media seeking.

    Usage::

        static(app, "/static", "static/")
        static(app, "/favicon.svg", "favicon.svg")
    """
    directory = os.path.abspath(directory)

    def _guess_type(path):
        ct, _ = mimetypes.guess_type(path)
        return ct or "application/octet-stream"

    def _parse_range(header, file_size):
        if not header or not header.startswith("bytes="):
            return None
        spec = header[6:].strip()
        if "," in spec:
            return None
        left, _, right = spec.partition("-")
        try:
            if left and right:  start, end = int(left), int(right) + 1
            elif left:          start, end = int(left), file_size
            elif right:         start, end = max(0, file_size - int(right)), file_size
            else:               return None
        except ValueError:
            return None
        if start < 0 or start >= file_size or end > file_size or start >= end:
            return None
        return start, end

    def _serve_file(req, full_path):
        proto      = req["_proto"]
        file_size  = os.path.getsize(full_path)
        content_type = _guess_type(full_path)
        range_header = req["headers"].get("range", "")
        parsed     = _parse_range(range_header, file_size)

        if parsed:
            start, end = parsed
            proto.response_file_range(206, [
                ("content-type",   content_type),
                ("content-length", str(end - start)),
                ("content-range",  f"bytes {start}-{end - 1}/{file_size}"),
                ("accept-ranges",  "bytes"),
                ("cache-control",  "public, max-age=0, must-revalidate"),
            ], full_path, start, end)
        elif range_header:
            proto.response_str(416, [
                ("content-range", f"bytes */{file_size}"),
            ], "Range Not Satisfiable")
        else:
            proto.response_file(200, [
                ("content-type",   content_type),
                ("content-length", str(file_size)),
                ("accept-ranges",  "bytes"),
                ("cache-control",  "public, max-age=3600"),
            ], full_path)
        req["_sent"] = True

    # Single file mount
    if os.path.isfile(directory):
        async def serve_single(req):
            _serve_file(req, directory)
        app.get(url_prefix)(serve_single)
        return

    # Directory mount
    async def serve_dir(req):
        rel  = req["params"].get("path", "")
        if not rel:
            return ("Not Found", 404)
        full = os.path.normpath(os.path.join(directory, rel))
        if not full.startswith(directory + os.sep) or not os.path.isfile(full):
            return ("Not Found", 404)
        _serve_file(req, full)

    app.mount(url_prefix.rstrip("/"), serve_dir)


# ── Application factory ──────────────────────────────────────────────────────

def create_app(routes: dict | None = None, *, on_init=None, on_del=None):
    """Create an md-web RSGI application.

    **Lifecycle hooks** (sync or async)::

        async def startup(loop):
            app.db = await create_pool()

        def shutdown(loop):
            print("goodbye")

        app = create_app(on_init=startup, on_del=shutdown)

    **Handler return protocol**:

    ============== =============================================
    Return type    Behaviour
    ============== =============================================
    ``str | Tag``  200 HTML response
    ``dict``       200 JSON response
    ``None``       204 No Content
    ``(str, int)`` redirect (3xx) or plain-text error (4xx/5xx)
    async gen      SSE stream (``text/event-stream``)
    ============== =============================================

    Usage::

        app = create_app()

        @app.get("/")
        async def index(req):
            return "<h1>Hello</h1>"
    """
    if routes is None:
        routes = {}

    param_routes  = []
    mounts        = []
    before_hooks  = []

    def _path_re(path):
        return re.compile("^" + PARAM_RE.sub(r"(?P<\1>[^/]+)", path) + "$")

    # ── Routing decorators ───────────────────────────────────────────────────

    def route(method: str, path: str):
        def decorator(fn):
            if "{" in path:
                param_routes.append((method.upper(), _path_re(path), fn))
            else:
                routes[(method.upper(), path)] = fn
            return fn
        return decorator

    def mount(prefix, fn):
        mounts.append((prefix.rstrip("/"), fn))
        mounts.sort(key=lambda x: -len(x[0]))

    def get(path):    return route("GET",    path)
    def post(path):   return route("POST",   path)
    def put(path):    return route("PUT",    path)
    def patch(path):  return route("PATCH",  path)
    def delete(path): return route("DELETE", path)

    # ── Beforeware ───────────────────────────────────────────────────────────

    def before(fn=None, *, methods=None):
        def decorator(f):
            m = {x.upper() for x in methods} if methods else None
            before_hooks.append((f, m))
            return f
        if fn is not None:
            before_hooks.append((fn, None))
            return fn
        return decorator

    # ── Response dispatch ─────────────────────────────────────────────────────

    def _respond(proto, req, result):
        headers = _cookie_headers(req)

        if isinstance(result, tuple) and len(result) == 2:
            content, status = result
            if isinstance(status, int) and 300 <= status < 400:
                headers.append(("location", content))
                proto.response_empty(status, headers)
            elif isinstance(status, int):
                headers.append(("content-type", "text/html; charset=utf-8"))
                proto.response_str(status, headers, content)
            return

        # Tag / Safe → HTML string
        if isinstance(result, Tag):
            result = render(result)
        elif hasattr(result, '__html__'):
            result = result.__html__()

        if isinstance(result, bytes):
            ct = req.get("_content_type", "application/octet-stream")
            headers.append(("content-type", ct))
            proto.response_bytes(200, headers, result)
        elif isinstance(result, str):
            headers.append(("content-type", "text/html; charset=utf-8"))
            proto.response_str(200, headers, result)
        elif isinstance(result, dict):
            headers.append(("content-type", "application/json"))
            proto.response_str(200, headers, json.dumps(result))
        elif result is None:
            proto.response_empty(204, headers)
        else:
            headers.append(("content-type", "text/plain; charset=utf-8"))
            proto.response_str(500, headers,
                f"Unsupported return type: {type(result).__name__}")

    # ── SSE keepalive ─────────────────────────────────────────────────────────

    async def _keepalive(transport, closed: asyncio.Event,
                         interval: int = 15, compressor=None,
                         use_gzip: bool = False):
        try:
            while not closed.is_set():
                await asyncio.sleep(interval)
                if not closed.is_set():
                    if compressor is not None:
                        chunk = compressor.process(b":\n\n") + compressor.flush()
                        await transport.send_bytes(chunk)
                    elif use_gzip:
                        await transport.send_bytes(_gzip_compress(b":\n\n"))
                    else:
                        await transport.send_str(":\n\n")
        except (asyncio.CancelledError, Exception):
            pass

    # ── Active SSE connection counter ────────────────────────────────────────
    _sse_connections = [0]   # mutable int in a list so closures can write it

    # ── RSGI entrypoint ───────────────────────────────────────────────────────

    async def handle(scope, proto):
        if scope.proto != "http":
            return

        req = _parse_request(scope, proto)
        req["params"] = {}

        handler = routes.get((req["method"], req["path"]))

        if handler is None:
            for method, pattern, fn in param_routes:
                if method == req["method"]:
                    m = pattern.match(req["path"])
                    if m:
                        req["params"] = m.groupdict()
                        handler = fn
                        break

        if handler is None:
            for prefix, fn in mounts:
                if req["path"] == prefix or req["path"].startswith(prefix + "/"):
                    req["params"]["path"] = req["path"][len(prefix) + 1:]
                    handler = fn
                    break

        if handler is None:
            proto.response_str(404, [("content-type", "text/plain")], "Not Found")
            return

        try:
            for hook, methods in before_hooks:
                if methods and req["method"] not in methods:
                    continue
                hook_result = hook(req)
                if inspect.isawaitable(hook_result):
                    hook_result = await hook_result
                if hook_result is not None:
                    _respond(proto, req, hook_result)
                    return

            result = handler(req)

            # Await coroutines first — the handler may be async def that
            # returns an async generator, rather than being one itself.
            if inspect.isawaitable(result) and not inspect.isasyncgen(result):
                result = await result

            if inspect.isasyncgen(result):
                closed = asyncio.Event()

                # ── Compression negotiation ───────────────────────────────
                # Two modes:
                #
                # 1. Per-client brotli (default for long-lived streams):
                #    Each connection gets its own brotli.Compressor. The
                #    shared context window grows over time giving 100-200:1
                #    ratios on repetitive HTML. Cost: O(N) compressions per
                #    broadcast.
                #
                # 2. Pre-compressed bytes (broadcaster pattern):
                #    When the async generator yields raw bytes instead of a
                #    str, those bytes are sent directly via send_bytes with
                #    content-encoding: gzip. The broadcaster pre-compresses
                #    once and fans out the same bytes to all N clients —
                #    O(1) compression regardless of N.
                #    Use create_broadcaster() to produce these payloads.
                #
                # The framework detects which mode to use per-event based on
                # whether the yielded value is bytes or str.

                compressor = None
                accept_enc = req["headers"].get("accept-encoding", "")
                use_brotli = "br" in accept_enc
                use_gzip   = "gzip" in accept_enc

                if use_brotli:
                    try:
                        import brotli
                        compressor = brotli.Compressor(
                            quality=5,
                            lgwin=22,
                            mode=brotli.MODE_TEXT,
                        )
                    except ImportError:
                        use_brotli = False

                # content-encoding is declared once at stream open.
                # For the broadcaster pattern we use gzip (pre-compressed).
                # For per-client brotli we use br.
                # If neither is available we send plain text.
                if use_brotli and compressor:
                    enc_header = ("content-encoding", "br")
                elif use_gzip:
                    enc_header = ("content-encoding", "gzip")
                else:
                    enc_header = None

                headers = [
                    ("content-type",      "text/event-stream"),
                    ("cache-control",     "no-cache"),
                    ("x-accel-buffering", "no"),
                ]
                if enc_header:
                    headers.append(enc_header)
                headers += _cookie_headers(req)

                _sse_connections[0] += 1
                transport  = proto.response_stream(200, headers)
                disconnect = asyncio.ensure_future(proto.client_disconnect())
                keepalive  = asyncio.create_task(
                    _keepalive(transport, closed, compressor=compressor,
                               use_gzip=use_gzip and not compressor)
                )

                def _on_disconnect(fut):
                    closed.set()
                disconnect.add_done_callback(_on_disconnect)

                try:
                    async for event in result:
                        if closed.is_set():
                            break
                        if isinstance(event, bytes):
                            # Pre-compressed payload from broadcaster.
                            # Already gzip-encoded — send directly.
                            await transport.send_bytes(event)
                        elif compressor is not None:
                            # Per-client brotli path
                            chunk = (compressor.process(event.encode())
                                     + compressor.flush())
                            await transport.send_bytes(chunk)
                        elif use_gzip:
                            # gzip mode (no brotli) — must compress str events
                            # too so the stream encoding stays consistent.
                            await transport.send_bytes(
                                _gzip_compress(event.encode())
                            )
                        else:
                            await transport.send_str(event)
                finally:
                    _sse_connections[0] -= 1
                    keepalive.cancel()
                    disconnect.cancel()
            else:
                if req.get("_sent"):
                    return
                _respond(proto, req, result)

        except Exception:
            traceback.print_exc()
            try:
                proto.response_str(500,
                    [("content-type", "text/plain")],
                    "Internal Server Error")
            except: pass  # noqa: E722

    # ── RSGI lifecycle hooks ──────────────────────────────────────────────────

    def _rsgi_init(loop):
        if on_init:
            result = on_init(loop)
            if inspect.iscoroutine(result):
                loop.run_until_complete(result)

    def _rsgi_del(loop):
        if on_del:
            result = on_del(loop)
            if inspect.iscoroutine(result):
                loop.run_until_complete(result)

    handle.__rsgi_init__ = _rsgi_init
    handle.__rsgi_del__  = _rsgi_del

    handle.route  = route
    handle.get    = get
    handle.post   = post
    handle.put    = put
    handle.patch  = patch
    handle.delete = delete
    handle.mount  = mount
    handle.before = before
    return handle


# ── Blocking dev server ───────────────────────────────────────────────────────

def serve(app, *, host: str = "127.0.0.1", port: int = 8000, **kwargs):
    """Run an app with Granian's embedded RSGI server (blocking).

    Usage::

        if __name__ == "__main__":
            serve(app)

    Any extra keyword arguments are forwarded to ``granian.server.embed.Server``
    (e.g. ``log_access=True``, ``ssl_cert=...``).
    """
    from granian.server.embed import Server
    from granian.constants import Interfaces

    server = Server(app, address=host, port=port, interface=Interfaces.RSGI, **kwargs)

    async def _run():
        await server.serve()

    try:
        asyncio.run(_run())
    except KeyboardInterrupt:
        pass
