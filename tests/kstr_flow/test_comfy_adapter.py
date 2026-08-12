import sys
import types

from kstr_flow.comfy_adapter import to_comfy_expansion
from kstr_flow.flow import compile_flow


def test_expansion_keeps_unreturned_output_nodes(registry, monkeypatch):
    graph_utils = types.ModuleType("comfy_execution.graph_utils")

    class GraphBuilder:
        @classmethod
        def alloc_prefix(cls):
            return "preview.0."

    def add_graph_prefix(graph, outputs, prefix):
        result = {}
        for node_id, info in graph.items():
            inputs = {}
            for key, value in info.get("inputs", {}).items():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], (int, float)):
                    inputs[key] = [prefix + value[0], value[1]]
                else:
                    inputs[key] = value
            result[prefix + node_id] = {"class_type": info["class_type"], "inputs": inputs}
        mapped_outputs = []
        for value in outputs:
            if isinstance(value, list) and len(value) == 2 and isinstance(value[0], str) and isinstance(value[1], (int, float)):
                mapped_outputs.append([prefix + value[0], value[1]])
            else:
                mapped_outputs.append(value)
        return result, tuple(mapped_outputs)

    graph_utils.GraphBuilder = GraphBuilder
    graph_utils.add_graph_prefix = add_graph_prefix
    package = types.ModuleType("comfy_execution")
    package.graph_utils = graph_utils
    monkeypatch.setitem(sys.modules, "comfy_execution", package)
    monkeypatch.setitem(sys.modules, "comfy_execution.graph_utils", graph_utils)

    source = '''
import debug

def workflow():
    debug.DebugOutput("hello")
    return 7
'''
    run = compile_flow(source, registry).run()
    expansion = to_comfy_expansion(run.ordered_outputs(), roots=(*run.ordered_outputs(), *run.output_roots))
    assert expansion["result"] == (7,)
    assert any(node["class_type"] == "DebugOutput" for node in expansion["expand"].values())
