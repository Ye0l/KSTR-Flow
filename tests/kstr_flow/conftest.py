import pytest

from kstr_flow.registry import FlowRegistry


@pytest.fixture
def object_info():
    return {
        "CheckpointLoaderSimple": {
            "input": {"required": {"ckpt_name": [["model.safetensors"]]}},
            "input_order": {"required": ["ckpt_name"]},
            "output": ["MODEL", "CLIP", "VAE"],
            "output_name": ["MODEL", "CLIP", "VAE"],
            "output_is_list": [False, False, False],
            "name": "CheckpointLoaderSimple",
            "display_name": "Load Checkpoint",
            "description": "",
            "python_module": "nodes",
            "category": "model/loaders",
            "output_node": False,
        },
        "CLIPTextEncode": {
            "input": {"required": {"text": ["STRING", {"multiline": True}], "clip": ["CLIP"]}},
            "input_order": {"required": ["text", "clip"]},
            "output": ["CONDITIONING"],
            "output_name": ["CONDITIONING"],
            "output_is_list": [False],
            "name": "CLIPTextEncode",
            "display_name": "CLIP Text Encode",
            "description": "",
            "python_module": "nodes",
            "category": "conditioning",
            "output_node": False,
        },
        "KSampler": {
            "input": {"required": {
                "model": ["MODEL"], "seed": ["INT", {"default": 0, "control_after_generate": True}],
                "steps": ["INT", {"default": 20}], "cfg": ["FLOAT", {"default": 8.0}],
                "positive": ["CONDITIONING"], "negative": ["CONDITIONING"]
            }},
            "input_order": {"required": ["model", "seed", "steps", "cfg", "positive", "negative"]},
            "output": ["LATENT"],
            "output_name": ["LATENT"],
            "output_is_list": [False],
            "name": "KSampler",
            "display_name": "KSampler",
            "description": "",
            "python_module": "nodes",
            "category": "sampling",
            "output_node": False,
        },
        "FaceDetailer": {
            "input": {"required": {"image": ["IMAGE"], "seed": ["INT", {"default": 0}]}},
            "input_order": {"required": ["image", "seed"]},
            "output": ["IMAGE", "SEGS"],
            "output_name": ["image", "segs"],
            "output_is_list": [False, False],
            "name": "FaceDetailer",
            "display_name": "FaceDetailer",
            "description": "",
            "python_module": "custom_nodes.ComfyUI-Impact-Pack.impact",
            "category": "ImpactPack",
            "output_node": False,
        },
    }


@pytest.fixture
def registry(object_info):
    return FlowRegistry.from_object_info(object_info)
