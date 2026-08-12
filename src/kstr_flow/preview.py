from __future__ import annotations

from typing import Any

from comfy_script.runtime import data

from .flow import compile_flow
from .lang.signature import MISSING, FlowPort
from .registry import FlowRegistry


_PRIMITIVE_DEFAULTS = {
    "INT": 0,
    "FLOAT": 0.0,
    "STRING": "",
    "BOOLEAN": False,
    "BOOL": False,
}


class PreviewInput(data.NodeOutput):
    def __init__(self, name: str, type_name: str):
        normalized = type_name if type_name and type_name != "ANY" else "*"
        info = {
            "name": "__KSTRInput",
            "input": {"required": {}},
            "output": [normalized],
            "output_name": [name],
        }
        prompt = {
            "class_type": "__KSTRInput",
            "inputs": {"name": name, "type": normalized},
        }
        super().__init__(info, prompt, 0)
        self.input_name = name
        self.preview_type = normalized


def _preview_value(port: FlowPort) -> Any:
    if port.default is not MISSING:
        return port.default
    simple_type = port.type_name.upper()
    if simple_type in _PRIMITIVE_DEFAULTS:
        return _PRIMITIVE_DEFAULTS[simple_type]
    return PreviewInput(port.name, port.type_name)


def _dedupe_roots(values):
    seen = set()
    roots = []
    for value in values:
        if not isinstance(value, data.NodeOutput):
            continue
        marker = id(value.node_prompt)
        if marker in seen:
            continue
        seen.add(marker)
        roots.append(value)
    return roots


def build_preview_graph(source: str, registry: FlowRegistry) -> dict[str, Any]:
    program = compile_flow(source, registry)
    inputs = {port.name: _preview_value(port) for port in program.signature.inputs}
    run = program.run(inputs, global_seed=0)
    returned = run.ordered_outputs()
    roots = _dedupe_roots((*run.call_roots, *(v for v in returned if isinstance(v, data.NodeOutput))))

    if roots:
        prompt, ids = data._get_outputs_prompt_and_id(roots)
    else:
        prompt, ids = {}, data.IdManager()

    graph_nodes: list[dict[str, Any]] = []
    graph_edges: list[dict[str, Any]] = []

    for node_id, node_prompt in prompt.items():
        class_type = node_prompt.get("class_type", "")
        if class_type == "__KSTRInput":
            name = str(node_prompt.get("inputs", {}).get("name", "input"))
            typ = str(node_prompt.get("inputs", {}).get("type", "*"))
            graph_nodes.append({
                "id": node_id,
                "kind": "input",
                "type": typ,
                "class_type": class_type,
                "label": name,
                "namespace": "input",
            })
        else:
            meta = registry.metadata.get(class_type)
            info = meta.info if meta else {}
            graph_nodes.append({
                "id": node_id,
                "kind": "node",
                "type": class_type,
                "class_type": class_type,
                "label": info.get("display_name", class_type),
                "namespace": meta.namespace if meta else "unknown",
                "category": info.get("category", ""),
                "deprecated": bool(info.get("deprecated", False)),
                "experimental": bool(info.get("experimental", False)),
            })

    prompt_ids = set(prompt)
    for target_id, node_prompt in prompt.items():
        for input_name, value in node_prompt.get("inputs", {}).items():
            if (
                isinstance(value, (list, tuple))
                and len(value) == 2
                and isinstance(value[0], str)
                and value[0] in prompt_ids
            ):
                graph_edges.append({
                    "from": value[0],
                    "from_slot": value[1],
                    "to": target_id,
                    "to_input": input_name,
                })

    for index, value in enumerate(returned):
        port = program.signature.outputs[index] if index < len(program.signature.outputs) else FlowPort(f"output_{index}")
        out_id = f"__KSTROutput.{index}"
        graph_nodes.append({
            "id": out_id,
            "kind": "output",
            "type": port.type_name,
            "class_type": "__KSTROutput",
            "label": port.name,
            "namespace": "output",
        })
        if isinstance(value, data.NodeOutput):
            source_id = ids.get_id(value.node_prompt)
            if source_id is not None:
                graph_edges.append({
                    "from": source_id,
                    "from_slot": value.output_slot,
                    "to": out_id,
                    "to_input": port.name,
                })

    return {
        "nodes": graph_nodes,
        "edges": graph_edges,
        "preview_inputs": {name: (value if not isinstance(value, PreviewInput) else None) for name, value in inputs.items()},
    }
