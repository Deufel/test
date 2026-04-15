"""md-web — a Datastar/RSGI web framework with Python HTML generation.

Submodules
----------
md_web.html     Tag construction and rendering
md_web.sse      Datastar SSE event formatters
md_web.app      RSGI application factory and request helpers
md_web.server   Background server utilities
md_web.tunnel   ngrok tunnel helpers

Flat imports
------------
Everything below is importable directly from ``md_web``::

    from md_web import create_app, patch_elements, Tag, render, Datastar

Dynamic tag creation
--------------------
Any attribute access that does not resolve to a real name returns a tag
factory, so you can write::

    from md_web import div, span, ul, li, h1
    page = div(cls="container")(h1("Hello"), span("world"))
"""
__version__ = "0.1.0"
__author__  = "Deufel"

# ── html ─────────────────────────────────────────────────────────────────────
from .html import (
    Safe,
    Tag,
    unpack,
    render_attrs,
    render,
    html_doc,
    mk_tag,
    html_to_tag,
    Datastar,
    MeCSS,
    Pointer,
    Favicon,
    heatmap,
)

# ── sse ──────────────────────────────────────────────────────────────────────
from .sse import (
    patch_elements,
    patch_signals,
    remove_signals,
    execute_script,
)

# ── app ──────────────────────────────────────────────────────────────────────
from .app import (
    body,
    header_values,
    body_stream,
    signals,
    set_cookie,
    create_relay,
    create_broadcaster,
    create_signer,
    static,
    create_app,
    serve,
)

# ── server ───────────────────────────────────────────────────────────────────
from .server import (
    ServerState,
    serve_background,
    stop_background,
    dev_alive,
    request_logger,
)

# ── tunnel ───────────────────────────────────────────────────────────────────
from .tunnel import (
    TunnelState,
    load_env,
    start_tunnel,
    stop_tunnel,
)

__all__ = [
    # html
    "Datastar", "Favicon", "MeCSS", "Pointer",
    "Safe", "Tag",
    "heatmap", "html_doc", "html_to_tag", "mk_tag",
    "render", "render_attrs", "unpack",
    # sse
    "execute_script", "patch_elements", "patch_signals", "remove_signals",
    # app
    "body", "body_stream", "create_app", "create_broadcaster", "create_relay", "create_signer",
    "header_values", "serve", "set_cookie", "signals", "static",
    # server
    "ServerState", "dev_alive", "request_logger", "serve_background", "stop_background",
    # tunnel
    "TunnelState", "load_env", "start_tunnel", "stop_tunnel",
]


# ── Dynamic tag factory ───────────────────────────────────────────────────────
# Allows `from md_web import div, span, h1` etc. without explicit exports.

def __getattr__(name: str):
    return mk_tag(name)
