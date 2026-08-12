from __future__ import annotations

import ast
from typing import Any

from .lang.signature import MISSING, FlowPort, parse_signature


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


def analyze_source(source: str) -> dict[str, Any]:
    try:
        tree = ast.parse(source, mode="exec")
        signature = parse_signature(tree)
        return {
            "ok": True,
            "inputs": [_port_dict(port) for port in signature.inputs],
            "outputs": [_port_dict(port) for port in signature.outputs],
            "error": None,
        }
    except (SyntaxError, ValueError, TypeError) as exc:
        return {
            "ok": False,
            "inputs": [],
            "outputs": [],
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "offset", None),
            },
        }
