from __future__ import annotations

import keyword
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Iterable

from comfy_script import astutil
from comfy_script.runtime import data
from comfy_script.runtime.nodes import Node, VirtualRuntimeFactory


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
    if not python_module or python_module == "nodes" or python_module.startswith("comfy_extras"):
        return "comfy"

    module = python_module.replace("\\", "/")
    if module.startswith("custom_nodes."):
        module = module[len("custom_nodes.") :]
    elif module.startswith("custom_nodes/"):
        module = module[len("custom_nodes/") :]

    # object_info may contain a submodule. The custom-node directory is the pack.
    pack = re.split(r"[./]", module, maxsplit=1)[0]
    return _identifier(pack)


class FlowSingleOutput(data.NodeOutput):
    """A single Comfy output that also exposes its declared name as an attribute."""

    def __init__(self, source: data.NodeOutput, output_name: str):
        super().__init__(source.node_info, source.node_prompt, source.output_slot)
        self.task = source.task
        self._output_name = astutil.str_to_class_id(output_name)

    def __getattr__(self, name: str):
        if name == self._output_name or name.upper() == self._output_name.upper():
            return self
        raise AttributeError(name)


class FlowOutputs(list[data.NodeOutput]):
    """Tuple-unpackable outputs with Comfy return names available as attributes."""

    def __init__(self, values: Iterable[data.NodeOutput], names: Iterable[str]):
        super().__init__(values)
        self._names: dict[str, int] = {}
        for index, name in enumerate(names):
            raw = astutil.str_to_class_id(str(name))
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


def _node_output_type(value: data.NodeOutput) -> Any:
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
        return value in expected
    if isinstance(value, data.NodeOutput):
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
    """ComfyScript virtual node with lightweight pre-graph type validation."""

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
                actual = _node_output_type(value) if isinstance(value, data.NodeOutput) else type(value).__name__
                raise TypeError(f"{self.info['name']}.{name}: expected {expected!r}, got {actual!r}")

        result = super().__call__(*args, **kwargs)
        names = list(self.info.get("output_name") or self.info.get("output") or [])
        if isinstance(result, list):
            return FlowOutputs(result, names)
        if isinstance(result, data.NodeOutput) and result.output_slot is not None and names:
            return FlowSingleOutput(result, names[0])
        return result


class FlowRuntimeFactory(VirtualRuntimeFactory):
    def new_node(self, info: dict, defaults: dict, output_types: list[type]):
        return FlowNode(info, defaults, output_types)


class NodeNamespace:
    def __init__(self, name: str):
        self.name = name
        self._nodes: dict[str, Any] = {}

    def add(self, raw_name: str, node: Any) -> None:
        aliases = {raw_name, astutil.str_to_class_id(raw_name)}
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
        self.factory = FlowRuntimeFactory()
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
        modules: dict[str, list[tuple[str, dict]]] = defaultdict(list)
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
            modules[python_module].append((raw_name, info))

        # Resolve namespace collisions deterministically per source module.
        used_aliases: dict[str, str] = {}
        for python_module in sorted(modules):
            base = normalize_python_module(python_module)
            alias = base
            i = 2
            while alias in used_aliases and used_aliases[alias] != python_module:
                alias = f"{base}{i}"
                i += 1
            used_aliases[alias] = python_module
            self._module_aliases[python_module] = alias
            self.namespaces.setdefault(alias, NodeNamespace(alias))

        for python_module, entries in modules.items():
            namespace = self._module_aliases[python_module]
            for raw_name, info in entries:
                self.factory.add_node(info)
                node = self.factory.nodes[raw_name]
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
            if astutil.str_to_class_id(raw) == name:
                candidates.append(node)
        if len(candidates) == 1:
            return candidates[0]
        if not candidates:
            raise NameError(f"Unknown ComfyUI node: {name}")
        raise NameError(f"Ambiguous ComfyUI node name: {name}; import its pack namespace")
