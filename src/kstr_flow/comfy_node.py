from __future__ import annotations

from typing_extensions import override

from comfy_api.latest import ComfyExtension, io

from .analyze import analyze_source
from . import comfy_routes as _comfy_routes  # registers PromptServer routes
from .comfy_adapter import to_comfy_expansion
from .flow import compile_flow
from .registry import get_runtime_registry


WEB_DIRECTORY = "./web"
MAX_OUTPUTS = 32


def _default_checkpoint_name() -> str:
    try:
        import folder_paths

        names = folder_paths.get_filename_list("checkpoints")
        if names:
            return str(names[0])
    except Exception:
        pass
    return "select_checkpoint.safetensors"


def _default_source() -> str:
    ckpt = repr(_default_checkpoint_name())
    return f'''import comfy


def workflow() -> IMAGE:
    model, clip, vae = comfy.CheckpointLoaderSimple(
        ckpt_name={ckpt},
    )

    positive = comfy.CLIPTextEncode(
        text="1girl, solo, masterpiece, best quality",
        clip=clip,
    )
    negative = comfy.CLIPTextEncode(
        text="low quality, worst quality",
        clip=clip,
    )

    latent = comfy.EmptyLatentImage(
        width=1024,
        height=1024,
        batch_size=1,
    )

    sampled = comfy.KSampler(
        model=model,
        seed=seed,
        steps=28,
        cfg=5.0,
        sampler_name="euler",
        scheduler="normal",
        positive=positive,
        negative=negative,
        latent_image=latent,
        denoise=1.0,
    )

    return comfy.VAEDecode(
        samples=sampled,
        vae=vae,
    )
'''


class KSTRFlowNode(io.ComfyNode):
    @classmethod
    def define_schema(cls) -> io.Schema:
        return io.Schema(
            node_id="KSTRFlow",
            display_name="KSTR Flow",
            category="KSTR/Flow",
            description="Write a ComfyUI workflow as safe Python-like code.",
            accept_all_inputs=True,
            enable_expand=True,
            inputs=[
                io.String.Input(
                    "source",
                    default=_default_source(),
                    multiline=True,
                    dynamic_prompts=False,
                    tooltip="KSTR Flow source code",
                ),
                io.Int.Input(
                    "global_seed",
                    default=0,
                    min=0,
                    max=0xFFFFFFFFFFFFFFFF,
                    control_after_generate=True,
                    tooltip="Execution seed exposed to the script as global_seed and seed.",
                ),
            ],
            outputs=[
                io.AnyType.Output(id=f"output_{index}", display_name=f"output {index}")
                for index in range(MAX_OUTPUTS)
            ],
        )

    @classmethod
    def validate_inputs(cls, source: str, global_seed: int, **kwargs):
        analysis = analyze_source(source)
        if not analysis["ok"]:
            return analysis["error"]["message"]
        if len(analysis["outputs"]) > MAX_OUTPUTS:
            return f"KSTR Flow supports at most {MAX_OUTPUTS} external outputs"
        return True

    @classmethod
    def execute(cls, source: str, global_seed: int, **kwargs) -> io.NodeOutput:
        registry = get_runtime_registry()
        program = compile_flow(source, registry)
        input_names = {port.name for port in program.signature.inputs}
        external_inputs = {name: value for name, value in kwargs.items() if name in input_names}
        run = program.run(external_inputs, global_seed=global_seed)
        outputs = run.ordered_outputs()
        if len(outputs) > MAX_OUTPUTS:
            raise RuntimeError(f"KSTR Flow produced {len(outputs)} outputs; maximum is {MAX_OUTPUTS}")

        expansion = to_comfy_expansion(outputs, roots=(*outputs, *run.output_roots))
        padded = tuple(expansion["result"]) + (None,) * (MAX_OUTPUTS - len(outputs))
        return io.NodeOutput(*padded, expand=expansion["expand"])


class KSTRFlowExtension(ComfyExtension):
    @override
    async def get_node_list(self) -> list[type[io.ComfyNode]]:
        return [KSTRFlowNode]


async def comfy_entrypoint() -> KSTRFlowExtension:
    return KSTRFlowExtension()
