from __future__ import annotations

from aiohttp import web
from server import PromptServer

from .analyze import analyze_source
from .registry import get_runtime_registry


@PromptServer.instance.routes.post("/kstr-flow/analyze")
async def kstr_flow_analyze(request):
    payload = await request.json()
    source = payload.get("source", "")
    if not isinstance(source, str):
        return web.json_response({"ok": False, "error": {"message": "source must be a string"}}, status=400)
    return web.json_response(analyze_source(source))


@PromptServer.instance.routes.get("/kstr-flow/registry")
async def kstr_flow_registry(request):
    registry = get_runtime_registry()
    packs: dict[str, list[dict]] = {}
    for raw_name, meta in registry.metadata.items():
        info = meta.info
        inputs = []
        for group in ("required", "optional"):
            for name, value in info.get("input", {}).get(group, {}).items():
                declared = value[0] if isinstance(value, (list, tuple)) and value else "*"
                inputs.append({
                    "name": name,
                    "type": declared if isinstance(declared, str) else "COMBO",
                    "optional": group == "optional",
                })
        packs.setdefault(meta.namespace, []).append({
            "name": raw_name,
            "display_name": info.get("display_name", raw_name),
            "inputs": inputs,
            "outputs": [
                {"name": name, "type": typ}
                for name, typ in zip(
                    info.get("output_name", info.get("output", [])),
                    info.get("output", []),
                )
            ],
            "deprecated": bool(info.get("deprecated", False)),
            "experimental": bool(info.get("experimental", False)),
        })
    for nodes in packs.values():
        nodes.sort(key=lambda item: item["name"].lower())
    return web.json_response({"packs": packs})
