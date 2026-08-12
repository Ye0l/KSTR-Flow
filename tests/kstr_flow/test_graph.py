from kstr_flow.graph import Node, NodeOutput, get_outputs_prompt_and_id


def test_node_defaults_and_graph_links():
    loader_info = {
        "name": "Loader",
        "input": {"required": {"name": [["a.safetensors", "b.safetensors"]]}},
        "output": ["MODEL"],
    }
    sampler_info = {
        "name": "Sampler",
        "input": {"required": {
            "model": ["MODEL"],
            "steps": ["INT", {"default": 20}],
        }},
        "output": ["LATENT"],
    }
    model = Node(loader_info)()
    latent = Node(sampler_info)(model=model)

    prompt, ids = get_outputs_prompt_and_id([latent])
    loader_id = ids.get_id(model.node_prompt)
    sampler_id = ids.get_id(latent.node_prompt)

    assert prompt[loader_id]["inputs"]["name"] == "a.safetensors"
    assert prompt[sampler_id]["inputs"]["steps"] == 20
    assert prompt[sampler_id]["inputs"]["model"] == [loader_id, 0]


def test_boolean_enum_mapping():
    info = {
        "name": "ToggleNode",
        "input": {"required": {"enabled": [["enable", "disable"]]}},
        "output": ["STRING"],
    }
    output = Node(info)(enabled=False)
    prompt, ids = get_outputs_prompt_and_id([output])
    node_id = ids.get_id(output.node_prompt)
    assert prompt[node_id]["inputs"]["enabled"] == "disable"


def test_nested_node_outputs_are_serialized():
    source = Node({"name": "Source", "input": {"required": {}}, "output": ["ITEM"]})()
    sink = Node({"name": "Sink", "input": {"required": {"items": ["*"]}}, "output": ["ITEM"]})(items=[source])
    prompt, ids = get_outputs_prompt_and_id([sink])
    assert prompt[ids.get_id(sink.node_prompt)]["inputs"]["items"] == [[ids.get_id(source.node_prompt), 0]]
