import pytest

from kstr_flow.registry import normalize_python_module


def test_namespace_normalization():
    assert normalize_python_module("nodes") == "comfy"
    assert normalize_python_module("comfy_extras.nodes_upscale_model") == "comfy"
    assert normalize_python_module("custom_nodes.ComfyUI-Impact-Pack.impact") == "impactpack"
    assert normalize_python_module("custom_nodes.ComfyUI-KSTR-Nodes.foo") == "kstr"
    assert normalize_python_module("custom_nodes.ComfyUI-Easy-Use.py") == "easyuse"


def test_registry_pack_and_named_outputs(registry):
    assert "comfy" in registry.namespaces
    assert "impactpack" in registry.namespaces
    model, clip, vae = registry.namespace("comfy").CheckpointLoaderSimple("model.safetensors")
    assert model.output_slot == 0
    result = registry.namespace("impactpack").FaceDetailer(image=object(), seed=1)
    assert result.image.output_slot == 0
    assert result.segs.output_slot == 1


def test_type_mismatch_is_rejected(registry):
    model, clip, _ = registry.namespace("comfy").CheckpointLoaderSimple("model.safetensors")
    with pytest.raises(TypeError, match="expected 'CLIP'"):
        registry.namespace("comfy").CLIPTextEncode(text="x", clip=model)
    assert registry.namespace("comfy").CLIPTextEncode(text="x", clip=clip).output_slot == 0


def test_core_and_pack_submodules_share_namespace():
    from kstr_flow.registry import FlowRegistry

    object_info = {
        "CoreA": {"python_module": "nodes", "input": {}, "output": []},
        "CoreB": {"python_module": "comfy_extras.nodes_foo", "input": {}, "output": []},
        "CoreC": {"python_module": "comfy_api.latest._io", "input": {}, "output": []},
        "ImpactA": {"python_module": "custom_nodes.ComfyUI-Impact-Pack.impact", "input": {}, "output": []},
        "ImpactB": {"python_module": "custom_nodes.ComfyUI-Impact-Pack.sub.foo", "input": {}, "output": []},
    }
    registry = FlowRegistry.from_object_info(object_info)
    assert set(registry.namespaces) == {"comfy", "impactpack"}
    assert registry.metadata["CoreA"].namespace == "comfy"
    assert registry.metadata["CoreB"].namespace == "comfy"
    assert registry.metadata["CoreC"].namespace == "comfy"
    assert registry.metadata["ImpactA"].namespace == "impactpack"
    assert registry.metadata["ImpactB"].namespace == "impactpack"


def test_large_combo_values_are_available_on_demand(registry):
    from kstr_flow.registry import input_combo_values

    registry.metadata["CheckpointLoaderSimple"].info["input"]["required"]["ckpt_name"] = (["a.safetensors", "b.safetensors"], {})
    assert input_combo_values(registry, "comfy", "CheckpointLoaderSimple", "ckpt_name") == ["a.safetensors", "b.safetensors"]
