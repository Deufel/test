"""Datastar Server-Sent Event formatters."""
import json
from .html import render, Tag


def patch_elements(
    elements,
    *,
    selector: str | None = None,
    mode: str | None = None,
    namespace: str | None = None,
    use_view_transition: bool | None = None,
) -> str:
    """Format a ``datastar-patch-elements`` SSE event.

    *elements* can be a :class:`~md_web.html.Tag`, a :class:`~md_web.html.Safe`
    string, or any plain string of HTML.
    """
    if isinstance(elements, Tag):
        elements = render(elements)
    elif hasattr(elements, '__html__'):
        elements = elements.__html__()

    lines = []
    if selector            is not None: lines.append(f"data: selector {selector}")
    if mode                is not None: lines.append(f"data: mode {mode}")
    if namespace           is not None: lines.append(f"data: namespace {namespace}")
    if use_view_transition is not None:
        lines.append(f"data: useViewTransition {str(use_view_transition).lower()}")

    for line in elements.split("\n"):
        lines.append(f"data: elements {line}")

    return "event: datastar-patch-elements\n" + "\n".join(lines) + "\n\n"


def patch_signals(signals: dict | str, *, only_if_missing: bool | None = None) -> str:
    """Format a ``datastar-patch-signals`` SSE event."""
    if isinstance(signals, dict):
        signals = json.dumps(signals)

    lines = []
    if only_if_missing is not None:
        lines.append(f"data: onlyIfMissing {str(only_if_missing).lower()}")
    lines.append(f"data: signals {signals}")

    return "event: datastar-patch-signals\n" + "\n".join(lines) + "\n\n"


def remove_signals(*names: str) -> str:
    """Remove one or more signals by patching them to ``null``."""
    return patch_signals({n: None for n in names})


def execute_script(
    script: str,
    *,
    auto_remove: bool = True,
    attributes: dict | None = None,
) -> str:
    """Format a ``datastar-execute-script`` SSE event."""
    lines = []
    if not auto_remove:        lines.append("data: autoRemove false")
    if attributes is not None: lines.append(f"data: attributes {json.dumps(attributes)}")

    for line in script.split("\n"):
        lines.append(f"data: script {line}")

    return "event: datastar-execute-script\n" + "\n".join(lines) + "\n\n"
