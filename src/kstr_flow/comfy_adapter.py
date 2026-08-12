from __future__ import annotations

from typing import Any, Iterable

from .graph import IdManager, NodeOutput, get_outputs_prompt_and_id


def _normalize_outputs(values: Iterable[Any]) -> tuple[Any, ...]:
    return tuple(values)


def to_comfy_expansion(values: Iterable[Any], *, roots: Iterable[Any] | None = None) -> dict:
    """Convert KSTR Flow outputs to a ComfyUI node-expansion return value.

    Imports ComfyUI lazily so the language/compiler remains unit-testable without a
    full ComfyUI checkout. ComfyUI's documented raw-graph expansion path accepts the
    KSTR Flow API-format graph.
    """
    from comfy_execution.graph_utils import GraphBuilder, add_graph_prefix

    outputs = _normalize_outputs(values)
    root_values = tuple(roots) if roots is not None else outputs
    node_roots: list[NodeOutput] = []
    seen_prompts: set[int] = set()
    for value in (*root_values, *outputs):
        if not isinstance(value, NodeOutput):
            continue
        marker = id(value.node_prompt)
        if marker in seen_prompts:
            continue
        seen_prompts.add(marker)
        node_roots.append(value)
    if node_roots:
        prompt, ids = get_outputs_prompt_and_id(node_roots)
    else:
        prompt, ids = {}, IdManager()

    raw_results = []
    for value in outputs:
        if isinstance(value, NodeOutput):
            node_id = ids.get_id(value.node_prompt)
            if node_id is None:
                raise RuntimeError("Failed to resolve KSTR Flow output node id")
            raw_results.append([node_id, value.output_slot])
        else:
            raw_results.append(value)

    prefix = GraphBuilder.alloc_prefix()
    graph, results = add_graph_prefix(prompt, raw_results, prefix)
    return {"result": tuple(results), "expand": graph}
