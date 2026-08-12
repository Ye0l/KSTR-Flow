from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any


MISSING = object()
RESERVED_NAMES = {"global_seed", "seed", "random", "math", "workflow", "env", "nodes"}


@dataclass(frozen=True)
class FlowPort:
    name: str
    type_name: str = "ANY"
    default: Any = MISSING

    @property
    def optional(self) -> bool:
        return self.default is not MISSING


@dataclass(frozen=True)
class FlowSignature:
    inputs: tuple[FlowPort, ...]
    outputs: tuple[FlowPort, ...]


def _annotation_name(annotation: ast.expr | None) -> str:
    if annotation is None:
        return "ANY"
    return ast.unparse(annotation)


def _literal_default(node: ast.expr) -> Any:
    try:
        return ast.literal_eval(node)
    except Exception as exc:
        raise SyntaxError("KSTR Flow input defaults must be literal values") from exc


def _return_ports(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[FlowPort, ...]:
    if fn.returns is not None:
        return_ann = fn.returns
        if isinstance(return_ann, ast.Tuple):
            return tuple(FlowPort(f"output_{i}", _annotation_name(t)) for i, t in enumerate(return_ann.elts))
        # Python's normal multi-output annotation is tuple[IMAGE, MASK].
        if (
            isinstance(return_ann, ast.Subscript)
            and isinstance(return_ann.value, ast.Name)
            and return_ann.value.id in {"tuple", "Tuple"}
            and isinstance(return_ann.slice, ast.Tuple)
        ):
            return tuple(FlowPort(f"output_{i}", _annotation_name(t)) for i, t in enumerate(return_ann.slice.elts))
        return (FlowPort("output", _annotation_name(return_ann)),)

    signatures: list[tuple[str, ...]] = []
    for node in ast.walk(fn):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Dict) and all(isinstance(k, ast.Constant) and isinstance(k.value, str) for k in value.keys):
            signatures.append(tuple(str(k.value) for k in value.keys))
        elif isinstance(value, (ast.Tuple, ast.List)):
            names = []
            for i, item in enumerate(value.elts):
                names.append(item.id if isinstance(item, ast.Name) else f"output_{i}")
            signatures.append(tuple(names))
        else:
            signatures.append((value.id if isinstance(value, ast.Name) else "output",))

    if not signatures:
        return ()
    first = signatures[0]
    if any(len(s) != len(first) for s in signatures[1:]):
        raise SyntaxError("All workflow return paths must have the same number of outputs")
    return tuple(FlowPort(name) for name in first)


def parse_signature(tree: ast.Module, function_name: str = "workflow") -> FlowSignature:
    functions = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == function_name]
    if len(functions) != 1:
        raise SyntaxError(f"KSTR Flow source must define exactly one {function_name}() function")
    fn = functions[0]
    if isinstance(fn, ast.AsyncFunctionDef):
        raise SyntaxError("async workflow functions are not supported")
    if fn.args.vararg or fn.args.kwarg or fn.args.kwonlyargs:
        raise SyntaxError("workflow() does not support *args, **kwargs, or keyword-only inputs yet")

    positional = list(fn.args.posonlyargs) + list(fn.args.args)
    defaults = [MISSING] * (len(positional) - len(fn.args.defaults)) + [_literal_default(v) for v in fn.args.defaults]
    inputs = []
    for arg, default in zip(positional, defaults):
        if arg.arg in RESERVED_NAMES:
            raise SyntaxError(f"{arg.arg!r} is a reserved KSTR Flow name")
        inputs.append(FlowPort(arg.arg, _annotation_name(arg.annotation), default))

    return FlowSignature(tuple(inputs), _return_ports(fn))
