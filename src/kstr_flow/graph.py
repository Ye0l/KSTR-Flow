from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import Any, Iterable


def is_bool_enum(values: Any) -> bool:
    if not isinstance(values, (list, tuple)) or len(values) != 2:
        return False
    if all(isinstance(v, bool) for v in values):
        return values[0] != values[1]
    if all(isinstance(v, str) for v in values):
        return {v.lower() for v in values} in (
            {"enable", "disable"}, {"on", "off"}, {"true", "false"}, {"yes", "no"}
        )
    return False


def _bool_enum_value(values: list[Any] | tuple[Any, ...], value: bool) -> Any:
    first = values[0]
    if isinstance(first, bool):
        return first if first is value else values[1]
    first_truthy = str(first).lower() in {"enable", "on", "true", "yes"}
    return values[0] if first_truthy == value else values[1]


def declared_input(info: dict, name: str) -> Any:
    for group in ("required", "optional"):
        entry = info.get("input", {}).get(group, {}).get(name)
        if entry is not None:
            return entry[0] if isinstance(entry, (list, tuple)) and entry else entry
    return None


def map_input(name: str, value: Any, info: dict) -> Any:
    declared = declared_input(info, name)
    if isinstance(value, bool) and is_bool_enum(declared):
        return _bool_enum_value(declared, value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, PurePath):
        return str(value)
    return value


def input_defaults(info: dict) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for group in ("required", "optional"):
        for name, entry in info.get("input", {}).get(group, {}).items():
            if not isinstance(entry, (list, tuple)) or not entry:
                continue
            declared = entry[0]
            config = entry[1] if len(entry) > 1 and isinstance(entry[1], dict) else {}
            if "default" in config:
                defaults[name] = config["default"]
                continue
            if declared == "COMBO":
                options = config.get("options") or []
                if options:
                    defaults[name] = options[0]
            elif isinstance(declared, (list, tuple)) and declared:
                defaults[name] = declared[0]
    return defaults


class IdManager:
    def __init__(self):
        self._type_ids: dict[str, int] = {}
        self._objid_id_map: dict[int, str] = {}
        self._id_obj_map: dict[str, dict] = {}

    def assign_id(self, node_prompt: dict) -> str:
        class_type = node_prompt["class_type"]
        type_id = self._type_ids.get(class_type, -1) + 1
        self._type_ids[class_type] = type_id
        node_id = f"{class_type}.{type_id}"
        self._objid_id_map[id(node_prompt)] = node_id
        self._id_obj_map[node_id] = node_prompt
        return node_id

    def get_id(self, node_prompt: dict) -> str | None:
        return self._objid_id_map.get(id(node_prompt))

    def get_obj(self, node_id: str) -> dict | None:
        return self._id_obj_map.get(node_id)


@dataclass
class NodeOutput:
    node_info: dict
    node_prompt: dict
    output_slot: int | None
    task: Any = None

    def _update_prompt(self, prompt: dict, ids: IdManager) -> str:
        existing = ids.get_id(self.node_prompt)
        if existing is not None:
            return existing

        serialized_inputs = {
            key: _serialize_value(value, prompt, ids, key, self.node_info)
            for key, value in self.node_prompt.get("inputs", {}).items()
        }
        node_id = ids.assign_id(self.node_prompt)
        prompt[node_id] = {
            "inputs": serialized_inputs,
            "class_type": self.node_prompt["class_type"],
        }
        return node_id

    def api_format(self) -> dict:
        prompt: dict = {}
        ids = IdManager()
        self._update_prompt(prompt, ids)
        return prompt


def _serialize_value(value: Any, prompt: dict, ids: IdManager, input_name: str, node_info: dict) -> Any:
    if isinstance(value, NodeOutput):
        return [value._update_prompt(prompt, ids), value.output_slot]
    if isinstance(value, list):
        return [_serialize_value(v, prompt, ids, input_name, node_info) for v in value]
    if isinstance(value, tuple):
        return [_serialize_value(v, prompt, ids, input_name, node_info) for v in value]
    if isinstance(value, dict):
        return {k: _serialize_value(v, prompt, ids, input_name, node_info) for k, v in value.items()}
    return map_input(input_name, value, node_info)


def get_outputs_prompt_and_id(outputs: Iterable[NodeOutput]) -> tuple[dict, IdManager]:
    prompt: dict = {}
    ids = IdManager()
    for output in outputs:
        output._update_prompt(prompt, ids)
    return prompt, ids


class Node:
    def __init__(self, info: dict):
        self.info = info
        self.defaults = input_defaults(info)

    def __call__(self, *args, **kwargs):
        positional_names: list[str] = []
        for group in ("required", "optional"):
            positional_names.extend(self.info.get("input", {}).get(group, {}).keys())
        if len(args) > len(positional_names):
            raise TypeError(
                f"{self.info['name']}() takes at most {len(positional_names)} positional arguments, got {len(args)}"
            )
        inputs = {name: value for name, value in zip(positional_names, args)}
        overlap = inputs.keys() & kwargs.keys()
        if overlap:
            name = next(iter(overlap))
            raise TypeError(f"{self.info['name']}() got multiple values for {name!r}")
        inputs.update(kwargs)
        inputs = {key: value for key, value in inputs.items() if value is not None}
        inputs = self.defaults | inputs
        node_prompt = {"inputs": inputs, "class_type": self.info["name"]}
        output_types = list(self.info.get("output", []))
        if not output_types:
            return NodeOutput(self.info, node_prompt, None)
        if len(output_types) == 1:
            return NodeOutput(self.info, node_prompt, 0)
        return [NodeOutput(self.info, node_prompt, index) for index in range(len(output_types))]

    def __repr__(self) -> str:
        return f"<KSTR Flow Node {self.info.get('name', '?')}>"
