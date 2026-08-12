from __future__ import annotations

import keyword
import contextvars
from contextlib import contextmanager
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from .graph import Node, NodeOutput, is_bool_enum
from .naming import str_to_class_id


_GENERIC_TRAILING = ("customnodes", "customnode", "nodes", "node")


def _identifier(value: str) -> str:
    value = re.sub(r"(?i)^comfyui[-_. ]*", "", value.strip())
    compact = re.sub(r"[^0-9A-Za-z]+", "", value).lower()
    for suffix in _GENERIC_TRAILING:
        if compact.endswith(suffix) and len(compact) > len(suffix):
            compact = compact[: -len(suffix)]
            break
    if not compact:
        compact = "pack"
    if compact[0].isdigit():
        compact = f"pack{compact}"
    if keyword.iskeyword(compact):
        compact += "_"
    return compact


def normalize_python_module(python_module: str | None) -> str:
    """Return the user-facing import namespace for a ComfyUI python_module."""
    if (
        not python_module
        or python_module == "nodes"
        or python_module.startswith("comfy_extras")
        or python_module.startswith("comfy_api")
    ):
        return "comfy"

    module = python_module.replace("\\", "/")
    if module.startswith("custom_nodes."):
        module = module[len("custom_nodes.") :]
    elif module.startswith("custom_nodes/"):
        module = module[len("custom_nodes/") :]

    # object_info may contain a submodule. The custom-node directory is the pack.
    pack = re.split(r"[./]", module, maxsplit=1)[0]
    return _identifier(pack)


def _module_pack_identity(python_module: str | None) -> tuple[str, str]:
    """Return the logical package behind an implementation module."""
    if (
        not python_module
        or python_module == "nodes"
        or python_module.startswith("comfy_extras")
        or python_module.startswith("comfy_api")
    ):
        return ("core", "comfy")

    module = python_module.replace("\\", "/")
    if module.startswith("custom_nodes."):
        rest = module[len("custom_nodes.") :]
        pack = re.split(r"[./]", rest, maxsplit=1)[0]
        return ("custom", pack.lower())
    if module.startswith("custom_nodes/"):
        rest = module[len("custom_nodes/") :]
        pack = re.split(r"[./]", rest, maxsplit=1)[0]
        return ("custom", pack.lower())

    root = re.split(r"[./]", module, maxsplit=1)[0]
    return ("module", root.lower())


_flow_call_collector: contextvars.ContextVar[list[NodeOutput] | None] = contextvars.ContextVar(
    "kstr_flow_call_collector", default=None
)
_flow_output_collector: contextvars.ContextVar[list[NodeOutput] | None] = contextvars.ContextVar(
    "kstr_flow_output_collector", default=None
)


def _root_output(value):
    if isinstance(value, NodeOutput):
        return value
    if isinstance(value, (list, tuple)):
        return next((item for item in value if isinstance(item, NodeOutput)), None)
    return None


@contextmanager
def capture_flow_calls():
    calls: list[NodeOutput] = []
    outputs: list[NodeOutput] = []
    call_token = _flow_call_collector.set(calls)
    output_token = _flow_output_collector.set(outputs)
    try:
        yield calls, outputs
    finally:
        _flow_call_collector.reset(call_token)
        _flow_output_collector.reset(output_token)


def _record_flow_call(value, *, output_node: bool = False) -> None:
    root = _root_output(value)
    if root is None:
        return
    calls = _flow_call_collector.get()
    if calls is not None:
        calls.append(root)
    if output_node:
        outputs = _flow_output_collector.get()
        if outputs is not None:
            outputs.append(root)


class FlowSingleOutput(NodeOutput):
    """A single Comfy output that also exposes its declared name as an attribute."""

    def __init__(self, source: NodeOutput, output_name: str):
        super().__init__(source.node_info, source.node_prompt, source.output_slot)
        self.task = source.task
        self._output_name = str_to_class_id(output_name)

    def __getattr__(self, name: str):
        if name == self._output_name or name.upper() == self._output_name.upper():
            return self
        raise AttributeError(name)


class FlowOutputs(list[NodeOutput]):
    """Tuple-unpackable outputs with Comfy return names available as attributes."""

    def __init__(self, values: Iterable[NodeOutput], names: Iterable[str]):
        super().__init__(values)
        self._names: dict[str, int] = {}
        for index, name in enumerate(names):
            raw = str_to_class_id(str(name))
            self._names[raw] = index
            self._names[raw.upper()] = index

    def __getattr__(self, name: str):
        index = self._names.get(name)
        if index is None:
            index = self._names.get(name.upper())
        if index is None:
            raise AttributeError(name)
        return self[index]


def _expected_input(info: dict, name: str):
    for group in ("required", "optional"):
        entry = info.get("input", {}).get(group, {}).get(name)
        if entry is not None:
            return entry[0] if isinstance(entry, (list, tuple)) else entry
    return None


def _node_output_type(value: NodeOutput) -> Any:
    slot = value.output_slot
    if slot is None:
        return None
    outputs = value.node_info.get("output", [])
    if 0 <= slot < len(outputs):
        return outputs[slot]
    return None


def _compatible(expected: Any, value: Any) -> bool:
    if expected in (None, "*", "ANY"):
        return True
    if isinstance(expected, (list, tuple)):
        if isinstance(value, bool) and is_bool_enum(expected):
            return True
        return value in expected
    if isinstance(value, NodeOutput):
        actual = _node_output_type(value)
        return expected == actual or expected == "*" or actual == "*"
    if expected == "INT":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "FLOAT":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "STRING":
        return isinstance(value, str)
    if expected in ("BOOLEAN", "BOOL"):
        return isinstance(value, bool)
    if expected == "COMBO":
        return True
    # Custom Comfy types are expected to arrive through links. Do not reject opaque
    # execution-time values because node expansion may receive them directly.
    return True


class FlowNode(Node):
    """KSTR Flow virtual node with lightweight pre-graph type validation."""

    def __call__(self, *args, **kwargs):
        positional = []
        for group in ("required", "optional"):
            positional.extend(self.info.get("input", {}).get(group, {}).keys())

        supplied = dict(kwargs)
        for index, value in enumerate(args):
            if index >= len(positional):
                break
            supplied.setdefault(positional[index], value)

        for name, value in supplied.items():
            if value is None:
                continue
            expected = _expected_input(self.info, name)
            if expected is not None and not _compatible(expected, value):
                actual = _node_output_type(value) if isinstance(value, NodeOutput) else type(value).__name__
                raise TypeError(f"{self.info['name']}.{name}: expected {expected!r}, got {actual!r}")

        result = super().__call__(*args, **kwargs)
        names = list(self.info.get("output_name") or self.info.get("output") or [])
        if isinstance(result, list):
            result = FlowOutputs(result, names)
        elif isinstance(result, NodeOutput) and result.output_slot is not None and names:
            result = FlowSingleOutput(result, names[0])
        _record_flow_call(result, output_node=bool(self.info.get("output_node", False)))
        return result


class NodeNamespace:
    def __init__(self, name: str):
        self.name = name
        self._nodes: dict[str, Any] = {}

    def add(self, raw_name: str, node: Any) -> None:
        aliases = {raw_name, str_to_class_id(raw_name)}
        for alias in aliases:
            self._nodes[alias] = node

    def __getattr__(self, name: str) -> Any:
        try:
            return self._nodes[name]
        except KeyError as exc:
            raise AttributeError(f"{self.name}.{name}") from exc

    def __dir__(self):
        return sorted(k for k in self._nodes if k.isidentifier())

    def get(self, name: str, default=None):
        return self._nodes.get(name, default)

    @property
    def nodes(self) -> dict[str, Any]:
        return dict(self._nodes)


@dataclass(frozen=True)
class RegistryNode:
    raw_name: str
    namespace: str
    python_module: str
    info: dict


class FlowRegistry:
    def __init__(self):
        self.nodes: dict[str, Any] = {}
        self.namespaces: dict[str, NodeNamespace] = {}
        self.metadata: dict[str, RegistryNode] = {}
        self._module_aliases: dict[str, str] = {}

    @classmethod
    def from_object_info(cls, object_info: dict[str, dict]) -> "FlowRegistry":
        registry = cls()
        registry.load_object_info(object_info)
        return registry

    def load_object_info(self, object_info: dict[str, dict]) -> None:
        packs: dict[tuple[str, str], list[tuple[str, dict, str]]] = defaultdict(list)
        for raw_name, original in object_info.items():
            info = dict(original)
            info.setdefault("name", raw_name)
            info.setdefault("display_name", raw_name)
            info.setdefault("description", "")
            info.setdefault("category", "sd")
            info.setdefault("output", [])
            info.setdefault("output_name", info["output"])
            info.setdefault("output_node", False)
            info.setdefault("input", {})
            python_module = info.get("python_module")
            if not python_module and info.get("_cls") is not None:
                python_module = getattr(info["_cls"], "__module__", "")
            python_module = python_module or "nodes"
            packs[_module_pack_identity(python_module)].append((raw_name, info, python_module))

        # Multiple implementation modules from one pack intentionally share one
        # import namespace. Only genuinely distinct packs compete for aliases.
        used_aliases: dict[str, tuple[str, str]] = {}
        pack_aliases: dict[tuple[str, str], str] = {}
        for identity in sorted(packs):
            entries = packs[identity]
            representative_module = entries[0][2]
            base = normalize_python_module(representative_module)
            alias = base
            i = 2
            while alias in used_aliases and used_aliases[alias] != identity:
                alias = f"{base}{i}"
                i += 1
            used_aliases[alias] = identity
            pack_aliases[identity] = alias
            self.namespaces.setdefault(alias, NodeNamespace(alias))
            for _, _, python_module in entries:
                self._module_aliases[python_module] = alias

        for identity, entries in packs.items():
            namespace = pack_aliases[identity]
            for raw_name, info, python_module in entries:
                node = FlowNode(info)
                self.nodes[raw_name] = node
                self.namespaces[namespace].add(raw_name, node)
                self.metadata[raw_name] = RegistryNode(raw_name, namespace, python_module, info)

    def namespace(self, name: str) -> NodeNamespace:
        try:
            return self.namespaces[name]
        except KeyError as exc:
            raise ImportError(f"Unknown ComfyUI node pack: {name}") from exc

    def namespace_for_module(self, python_module: str) -> str | None:
        return self._module_aliases.get(python_module)

    def resolve(self, name: str) -> Any:
        if name in self.nodes:
            return self.nodes[name]
        candidates = []
        for raw, node in self.nodes.items():
            if str_to_class_id(raw) == name:
                candidates.append(node)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise NameError(f"Unknown ComfyUI node: {name}")
        raise NameError(f"Ambiguous ComfyUI node name: {name}; import its pack namespace")


def collect_comfy_object_info() -> dict[str, dict]:
    """Collect `/object_info`-equivalent metadata inside a running ComfyUI process.

    This avoids an HTTP round-trip to the same ComfyUI server and supports both
    V1 nodes and V3 schema nodes.
    """
    import nodes

    result: dict[str, dict] = {}
    for node_id, obj_class in nodes.NODE_CLASS_MAPPINGS.items():
        try:
            if hasattr(obj_class, "GET_NODE_INFO_V1"):
                info = obj_class.GET_NODE_INFO_V1()
            else:
                inputs = obj_class.INPUT_TYPES()
                info = {
                    "input": inputs,
                    "input_order": {key: list(value.keys()) for key, value in inputs.items()},
                    "is_input_list": getattr(obj_class, "INPUT_IS_LIST", False),
                    "output": list(obj_class.RETURN_TYPES),
                    "output_is_list": list(getattr(obj_class, "OUTPUT_IS_LIST", [False] * len(obj_class.RETURN_TYPES))),
                    "output_name": list(getattr(obj_class, "RETURN_NAMES", obj_class.RETURN_TYPES)),
                    "name": node_id,
                    "display_name": nodes.NODE_DISPLAY_NAME_MAPPINGS.get(node_id, node_id),
                    "description": getattr(obj_class, "DESCRIPTION", ""),
                    "python_module": getattr(obj_class, "RELATIVE_PYTHON_MODULE", "nodes"),
                    "category": getattr(obj_class, "CATEGORY", "sd"),
                    "output_node": bool(getattr(obj_class, "OUTPUT_NODE", False)),
                    "has_intermediate_output": bool(getattr(obj_class, "HAS_INTERMEDIATE_OUTPUT", False)),
                    "search_aliases": list(getattr(obj_class, "SEARCH_ALIASES", [])),
                }
                if getattr(obj_class, "DEPRECATED", False):
                    info["deprecated"] = True
                if getattr(obj_class, "EXPERIMENTAL", False):
                    info["experimental"] = True
                if getattr(obj_class, "DEV_ONLY", False):
                    info["dev_only"] = True
            info = dict(info)
            info.setdefault("name", node_id)
            # ComfyUI's loader knows the actual origin pack. V3 schema metadata
            # can report an implementation module such as comfy_api.*, which is
            # not a useful user-facing namespace and may collapse custom packs.
            relative_module = getattr(obj_class, "RELATIVE_PYTHON_MODULE", None)
            if relative_module:
                info["python_module"] = relative_module
            else:
                info.setdefault("python_module", "nodes")
            result[node_id] = info
        except Exception:
            # Match ComfyUI's /object_info behavior: one broken node should not make
            # the entire registry unusable.
            continue
    return result


_runtime_registry: FlowRegistry | None = None


def get_runtime_registry(*, refresh: bool = False) -> FlowRegistry:
    global _runtime_registry
    if refresh or _runtime_registry is None:
        _runtime_registry = FlowRegistry.from_object_info(collect_comfy_object_info())
    return _runtime_registry
