from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Any

from comfy_script.runtime import data

from .lang.evaluator import SafeEvaluator
from .lang.signature import FlowSignature, parse_signature
from .registry import FlowRegistry


@dataclass
class FlowRun:
    result: Any
    global_seed: int

    def ordered_outputs(self) -> tuple[Any, ...]:
        if self.result is None:
            return ()
        if isinstance(self.result, dict):
            return tuple(self.result.values())
        if isinstance(self.result, (tuple, list)) and not isinstance(self.result, data.NodeOutput):
            return tuple(self.result)
        return (self.result,)


class FlowProgram:
    def __init__(self, source: str, registry: FlowRegistry):
        self.source = source
        self.registry = registry
        self.tree = ast.parse(source, mode="exec")
        self.signature: FlowSignature = parse_signature(self.tree)

    def run(self, inputs: dict[str, Any] | None = None, *, global_seed: int = 0) -> FlowRun:
        inputs = dict(inputs or {})
        expected = {port.name for port in self.signature.inputs}
        unknown = set(inputs) - expected
        if unknown:
            raise TypeError(f"Unknown workflow inputs: {', '.join(sorted(unknown))}")

        evaluator = SafeEvaluator(self.registry, global_seed)
        env = evaluator.prepare(self.tree)
        workflow = env.resolve("workflow")
        kwargs = {}
        for port in self.signature.inputs:
            if port.name in inputs:
                kwargs[port.name] = inputs[port.name]
        return FlowRun(workflow(**kwargs), int(global_seed))


def compile_flow(source: str, registry: FlowRegistry) -> FlowProgram:
    return FlowProgram(source, registry)
