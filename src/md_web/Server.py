"""Background server utilities (Granian embed)."""
import asyncio
import socket
import threading
from dataclasses import dataclass


@dataclass
class ServerState:
    """Handle returned by :func:`serve_background`, passed to :func:`stop_background`."""
    server: object = None
    loop:   object = None
    thread: object = None


def serve_background(app, host: str = "127.0.0.1", port: int = 8000, **kwargs) -> ServerState:
    """Run an md-web app in a background thread.

    Returns a :class:`ServerState` handle; pass it to :func:`stop_background`
    to shut the server down cleanly.

    Usage::

        state = serve_background(app)
        # ... do work ...
        stop_background(state)

    Any extra keyword arguments are forwarded to ``granian.server.embed.Server``.
    """
    from granian.server.embed import Server
    from granian.constants import Interfaces

    server = Server(app, address=host, port=port, interface=Interfaces.RSGI, **kwargs)
    loop   = asyncio.new_event_loop()

    async def run():
        await server.serve()

    def thread_target():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(run())

    thread = threading.Thread(target=thread_target, daemon=True)
    thread.start()
    return ServerState(server=server, loop=loop, thread=thread)


def stop_background(state: ServerState) -> None:
    """Stop a server started with :func:`serve_background`."""
    if state.server and state.loop and state.loop.is_running():
        state.loop.call_soon_threadsafe(state.server.stop)
    if state.thread:
        state.thread.join(timeout=3)


def dev_alive(port: int = 8000) -> bool:
    """Return ``True`` if something is listening on *port* on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("localhost", port)) == 0


def request_logger(relay, topic: str = "dev.request"):
    """Return a beforeware hook that publishes each request to *relay*.

    Usage::

        relay = create_relay()
        app.before(request_logger(relay))
    """
    def hook(req):
        relay.publish(topic, f"{req['method']} {req['path']}")
    return hook
