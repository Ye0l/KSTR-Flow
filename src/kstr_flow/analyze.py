from __future__ import annotations

import ast
from typing import Any, TYPE_CHECKING

from .lang.signature import MISSING, FlowPort, parse_signature

if TYPE_CHECKING:
    from .registry import FlowRegistry


def _json_default(value: Any):
    if value is MISSING:
        return None
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return list(value)
    if isinstance(value, dict):
        return value
    return repr(value)


def _port_dict(port: FlowPort) -> dict[str, Any]:
    return {
        "name": port.name,
        "type": port.type_name,
        "optional": port.optional,
        "has_default": port.default is not MISSING,
        "default": _json_default(port.default),
    }


def analyze_source(source: str, registry: "FlowRegistry | None" = None) -> dict[str, Any]:
    try:
        tree = ast.parse(source, mode="exec")
        signature = parse_signature(tree)
        result = {
            "ok": True,
            "inputs": [_port_dict(port) for port in signature.inputs],
            "outputs": [_port_dict(port) for port in signature.outputs],
            "error": None,
            "graph": None,
            "graph_error": None,
        }
        if registry is not None:
            try:
                from .preview import build_preview_graph
                result["graph"] = build_preview_graph(source, registry)
            except Exception as exc:
                # Graph preview is best-effort. A runtime-dependent branch may not
                # be resolvable from defaults/placeholders, but that must not make
                # otherwise valid source impossible to edit.
                result["graph_error"] = f"{type(exc).__name__}: {exc}"
        return result
    except (SyntaxError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            "inputs": [],
            "outputs": [],
            "graph": None,
            "graph_error": None,
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "offset", None),
            },
        }
