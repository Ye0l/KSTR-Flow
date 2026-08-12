import pytest

from kstr_flow.flow import compile_flow
from kstr_flow.lang.evaluator import FlowSecurityError


def test_pythonic_flow_builds_graph_and_uses_seed(registry):
    source = '''
import comfy

def build_prompt(tags):
    return ", ".join([tag.strip() for tag in tags if tag.strip()])

def workflow(prompt: STRING = "girl"):
    model, clip, vae = comfy.CheckpointLoaderSimple("model.safetensors")
    tags = ["masterpiece", prompt, "solo"]
    positive = comfy.CLIPTextEncode(build_prompt(tags), clip)
    negative = comfy.CLIPTextEncode("lowres", clip)
    steps = 20 + 4 * 2
    latent = comfy.KSampler(model, seed, steps, 5.5, positive, negative)
    return latent
'''
    program = compile_flow(source, registry)
    assert [p.name for p in program.signature.inputs] == ["prompt"]
    run = program.run({"prompt": "1girl"}, global_seed=1234)
    [latent] = run.ordered_outputs()
    prompt = latent.api_format()
    sampler = next(v for v in prompt.values() if v["class_type"] == "KSampler")
    assert sampler["inputs"]["seed"] == 1234
    assert sampler["inputs"]["steps"] == 28


def test_seeded_random_is_reproducible(registry):
    source = '''
def workflow():
    values = [random.randint(0, 1000000) for _ in range(4)]
    return values
'''
    program = compile_flow(source, registry)
    assert program.run(global_seed=99).result == program.run(global_seed=99).result
    assert program.run(global_seed=99).result != program.run(global_seed=100).result


def test_for_if_dict_and_map(registry):
    source = '''
def workflow():
    cfg = {"base": 4, "extra": 2}
    values = []
    for i in range(5):
        if i % 2 == 0:
            values.append(i * cfg["extra"])
    return list(map(str, values))
'''
    assert compile_flow(source, registry).run().result == ["0", "4", "8"]


def test_arbitrary_import_and_dunder_are_blocked(registry):
    with pytest.raises(ImportError):
        compile_flow("import os\ndef workflow():\n    return 1", registry).run()
    with pytest.raises(FlowSecurityError):
        compile_flow("def workflow():\n    return (1).__class__", registry).run()


def test_reserved_seed_cannot_be_external_port(registry):
    with pytest.raises(SyntaxError, match="reserved"):
        compile_flow("def workflow(seed: INT):\n    return seed", registry)
