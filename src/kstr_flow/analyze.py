from __future__ import annotations

import ast
from typing import Any, TYPE_CHECKING

from .lang.signature import MISSING, FlowPort, parse_signature
from .naming import str_to_class_id

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




def _annotation_name(node: ast.expr | None) -> str | None:
    if node is None:
        return None
    try:
        return ast.unparse(node)
    except Exception:
        return None


def _node_call_info(call: ast.Call, registry: "FlowRegistry", imports: dict[str, str]):
    func = call.func
    if not isinstance(func, ast.Attribute) or not isinstance(func.value, ast.Name):
        return None
    alias = func.value.id
    namespace = imports.get(alias, alias)
    for meta in registry.metadata.values():
        if meta.namespace != namespace:
            continue
        if meta.raw_name == func.attr or str_to_class_id(meta.raw_name) == func.attr:
            return meta
    return None


def _infer_symbols(tree: ast.Module, registry: "FlowRegistry") -> dict[str, dict[str, Any]]:
    """Best-effort static types for editor hover information.

    This intentionally follows only assignments that can be resolved without
    executing user code. Runtime validation remains the source of truth.
    """
    imports: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Import):
            for alias in stmt.names:
                imports[alias.asname or alias.name] = alias.name

    workflow = next(
        (stmt for stmt in tree.body if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)) and stmt.name == "workflow"),
        None,
    )
    if workflow is None:
        return {}

    symbols: dict[str, dict[str, Any]] = {}
    for arg in [*workflow.args.posonlyargs, *workflow.args.args, *workflow.args.kwonlyargs]:
        type_name = _annotation_name(arg.annotation)
        if type_name:
            symbols[arg.arg] = {"type": type_name, "kind": "input"}

    def bind_target(target: ast.expr, types: list[str], *, kind: str = "variable") -> None:
        if isinstance(target, ast.Name):
            if len(types) == 1:
                symbols[target.id] = {"type": types[0], "kind": kind}
            elif types:
                symbols[target.id] = {"type": f"tuple[{', '.join(types)}]", "kind": kind}
        elif isinstance(target, (ast.Tuple, ast.List)):
            for index, element in enumerate(target.elts):
                if isinstance(element, ast.Name) and index < len(types):
                    symbols[element.id] = {"type": types[index], "kind": kind}

    for stmt in ast.walk(workflow):
        if isinstance(stmt, ast.Assign):
            value_types: list[str] = []
            if isinstance(stmt.value, ast.Call):
                meta = _node_call_info(stmt.value, registry, imports)
                if meta is not None:
                    value_types = [str(value) for value in (meta.info.get("output") or [])]
            elif isinstance(stmt.value, ast.Name) and stmt.value.id in symbols:
                value_types = [symbols[stmt.value.id]["type"]]
            if value_types:
                for target in stmt.targets:
                    bind_target(target, value_types, kind="node_output")
        elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
            type_name = _annotation_name(stmt.annotation)
            if type_name:
                symbols[stmt.target.id] = {"type": type_name, "kind": "variable"}

    return symbols


def analyze_source(
    source: str,
    registry: "FlowRegistry | None" = None,
    *,
    include_preview: bool = True,
) -> dict[str, Any]:
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
            "symbols": {},
        }
        if registry is not None:
            result["symbols"] = _infer_symbols(tree, registry)
            if include_preview:
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
            "symbols": {},
            "error": {
                "type": type(exc).__name__,
                "message": str(exc),
                "line": getattr(exc, "lineno", None),
                "column": getattr(exc, "offset", None),
            },
        }
