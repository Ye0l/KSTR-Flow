# KSTR Flow

Text-first ComfyUI workflows inside a single ComfyUI node.

KSTR Flow embeds a Python-like editor in ComfyUI. Installed ComfyUI nodes are exposed as callable functions, while ordinary control flow and value manipulation stay in code instead of becoming utility nodes on the canvas.

> Status: early prototype. The core language, dynamic I/O, autocomplete, graph preview, and ComfyUI expansion path are implemented. Real-world ComfyUI smoke testing is still in progress.

> The `KSTRFlow` node now also ships inside [ComfyUI-KSTR-Nodes](https://github.com/Ye0l/ComfyUI-KSTR-Nodes). Install one or the other, not both: they register the same node ID and the same `/kstr-flow/*` endpoints.

## Install for testing

Use the same Python environment that runs ComfyUI.

```bash
cd /path/to/ComfyUI/custom_nodes
git clone https://github.com/Ye0l/KSTR-Flow.git
cd KSTR-Flow
python -m pip install -r requirements.txt
```

Restart ComfyUI. Add **KSTR Flow** from `KSTR / Flow`.

If the repository is already cloned:

```bash
cd /path/to/ComfyUI/custom_nodes/KSTR-Flow
git pull
python -m pip install -r requirements.txt
```

No frontend build is required for normal use; the CodeMirror bundle is committed in the repository.

## Current syntax

```python
import comfy
import impactpack


def workflow(image: IMAGE, prompt: STRING = "1girl") -> IMAGE:
    model, clip, vae = comfy.CheckpointLoaderSimple(
        "model.safetensors"
    )

    tags = ["masterpiece", prompt, "solo"]
    text = ", ".join(tag for tag in tags if tag)

    positive = comfy.CLIPTextEncode(text, clip)
    negative = comfy.CLIPTextEncode("lowres", clip)

    steps = 28 if len(tags) > 2 else 20

    latent = comfy.KSampler(
        model=model,
        seed=seed,
        steps=steps,
        cfg=5.5,
        positive=positive,
        negative=negative,
        # latent_image=...
    )

    detailed = impactpack.FaceDetailer(
        image=image,
        seed=global_seed + 1,
    )

    return detailed.image
```

`workflow()` parameters become external KSTR Flow inputs. Return annotations/values become external outputs. The backend uses generic ComfyUI sockets while the editor performs type-aware analysis from the currently installed node registry.

## Implemented

- CodeMirror 6 editor embedded in the node
- installed-node registry and pack namespaces (`comfy`, `impactpack`, etc.)
- node/function autocomplete and input metadata
- dynamic external inputs and outputs from `workflow()`
- `global_seed` and `seed` reserved values
- deterministic `random` namespace and safe `math` namespace
- `if` / `elif` / `else`
- `for`, `break`, `continue`
- `match` / `case`
- functions and return values
- list / tuple / dict / set
- comprehensions, `map`, `filter`, `zip`, `range`, `enumerate`
- arithmetic, comparisons, boolean expressions, f-strings and common string/container methods
- lightweight type checks between ComfyUI node connections
- read-only compiled DAG preview above the editor
- expansion into real ComfyUI execution nodes
- output-node roots such as `SaveImage(...)` are retained even when not returned

## Not finished yet

- full live smoke test against a production ComfyUI install with large custom-node collections
- complete static diagnostics for every node call before execution
- code ↔ graph click/highlight navigation
- existing ComfyUI workflow → KSTR Flow source import
- richer hover documentation and very large enum/model completion UX
- polished handling of primitive external inputs as native widgets

## Security model

KSTR Flow does **not** execute arbitrary Python with `exec()`.

The source is parsed as Python syntax and evaluated by a restricted AST interpreter. Filesystem, subprocess, networking, arbitrary Python modules, reflection, private/dunder attributes, `eval`, and `exec` are not exposed. `import` resolves only KSTR Flow safe namespaces and installed ComfyUI node-pack namespaces.

## Development

Python tests:

```bash
python -m pip install pytest
python -m pytest tests/kstr_flow -q
```

Frontend bundle:

```bash
cd frontend
npm ci
npm run build
```

## License

MIT. KSTR Flow started as a fork of ComfyScript by Chaoses-Ib; the original MIT notice is retained in `LICENSE.txt`.
