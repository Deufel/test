"""ngrok tunnel helpers for exposing a local server to the internet."""
import os
import threading
from dataclasses import dataclass


@dataclass
class TunnelState:
    """Handle returned by :func:`start_tunnel`, passed to :func:`stop_tunnel`."""
    listener: object = None
    url:      str    = ""


def load_env(path: str = ".env") -> None:
    """Load a ``.env`` file into :data:`os.environ` (``setdefault`` semantics).

    Existing environment variables are never overwritten.
    """
    for line in open(path):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def start_tunnel(port: int = 8000, **kwargs) -> TunnelState:
    """Open an ngrok tunnel to ``localhost:<port>``.

    Requires the ``ngrok`` package and the ``NGROK_AUTHTOKEN`` environment
    variable (or call :func:`load_env` first).

    Extra keyword arguments are forwarded to ``ngrok.forward()``.

    Usage::

        tunnel = start_tunnel(8000)
        print(tunnel.url)
        # later …
        stop_tunnel(tunnel)
    """
    import ngrok

    result = [None]

    def _connect():
        result[0] = ngrok.forward(port, authtoken_from_env=True, **kwargs)

    t = threading.Thread(target=_connect)
    t.start()
    t.join()

    listener = result[0]
    return TunnelState(listener=listener, url=listener.url())


def stop_tunnel(tunnel: TunnelState) -> None:
    """Close an ngrok tunnel opened with :func:`start_tunnel`."""
    if tunnel.listener:
        import ngrok
        ngrok.disconnect(tunnel.url)
