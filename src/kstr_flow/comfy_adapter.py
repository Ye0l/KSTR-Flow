from __future__ import annotations

from typing import Any, Iterable

from comfy_script.runtime import data


def _normalize_outputs(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(values)


def to_comfy_expansion(values: Iterable[Any]) -> dict:
    """Convert KSTR/ComfyScript outputs to a ComfyUI node-expansion return value.

    Imports ComfyUI lazily so the language/compiler remains unit-testable without a
    full ComfyUI checkout. ComfyUI's documented raw-graph expansion path accepts the
    same API-format graph ComfyScript already builds.
    """
    from comfy_execution.graph_utils import GraphBuilder, add_graph_prefix

    outputs = _normalize_outputs(values)
    node_outputs = [v for v in outputs if isinstance(v, data.NodeOutput)]
    if node_outputs:
        prompt, ids = data._get_outputs_prompt_and_id(node_outputs)
    else:
        prompt, ids = {}, data.IdManager()

    raw_results = []
    for value in outputs:
        if isinstance(value, data.NodeOutput):
            node_id = ids.get_id(value.node_prompt)
            if node_id is None:
                raise RuntimeError("Failed to resolve KSTR Flow output node id")
            raw_results.append([node_id, value.output_slot])
        else:
            raw_results.append(value)

    prefix = GraphBuilder.alloc_prefix()
    graph, results = add_graph_prefix(prompt, raw_results, prefix)
    return {"result": tuple(results), "expand": graph}
