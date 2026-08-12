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
