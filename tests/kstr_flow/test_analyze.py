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
