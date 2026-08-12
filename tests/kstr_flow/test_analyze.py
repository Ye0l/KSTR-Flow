from kstr_flow.analyze import analyze_source


def test_analyze_dynamic_ports():
    result = analyze_source('''
def workflow(image: IMAGE, strength: FLOAT = 0.5) -> tuple[IMAGE, MASK]:
    return image, image
''')
    assert result["ok"] is True
    assert result["inputs"] == [
        {"name": "image", "type": "IMAGE", "optional": False, "has_default": False, "default": None},
        {"name": "strength", "type": "FLOAT", "optional": True, "has_default": True, "default": 0.5},
    ]
    assert [port["type"] for port in result["outputs"]] == ["IMAGE", "MASK"]


def test_analyze_reports_syntax_error():
    result = analyze_source("def workflow(:")
    assert result["ok"] is False
    assert result["error"]["type"] == "SyntaxError"


def test_analyze_builds_preview_graph(registry):
    source = '''
import comfy
import impactpack

def workflow(image: IMAGE, prompt: STRING = "1girl") -> IMAGE:
    model, clip, vae = comfy.CheckpointLoaderSimple("model.safetensors")
    cond = comfy.CLIPTextEncode(prompt, clip)
    detail = impactpack.FaceDetailer(image=image, seed=seed)
    return detail.image
'''
    result = analyze_source(source, registry)
    assert result["ok"] is True
    assert result["graph_error"] is None
    graph = result["graph"]
    kinds = {node["kind"] for node in graph["nodes"]}
    assert {"input", "node", "output"}.issubset(kinds)
    class_types = {node["class_type"] for node in graph["nodes"]}
    assert "CheckpointLoaderSimple" in class_types
    assert "CLIPTextEncode" in class_types
    assert "FaceDetailer" in class_types
    assert any(edge["to_input"] == "image" for edge in graph["edges"])
