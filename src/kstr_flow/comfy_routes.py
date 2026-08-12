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
    return web.json_response(analyze_source(source, get_runtime_registry()))


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
                config = value[1] if isinstance(value, (list, tuple)) and len(value) > 1 and isinstance(value[1], dict) else {}
                is_combo = isinstance(declared, (list, tuple))
                combo_values = list(declared) if is_combo else None
                # Model/LoRA selectors can contain thousands of values. Shipping all
                # of those just to complete node names would recreate /object_info's
                # multi-megabyte payload. Small enums remain useful for hints.
                inputs.append({
                    "name": name,
                    "type": "COMBO" if is_combo else (declared if isinstance(declared, str) else "*"),
                    "optional": group == "optional",
                    "has_default": "default" in config,
                    "default": config.get("default"),
                    "options": combo_values if combo_values is not None and len(combo_values) <= 128 else None,
                    "option_count": len(combo_values) if combo_values is not None else None,
                    "tooltip": config.get("tooltip"),
                    "advanced": bool(config.get("advanced", False)),
                })
        packs.setdefault(meta.namespace, []).append({
            "name": raw_name,
            "display_name": info.get("display_name", raw_name),
            "description": info.get("description", ""),
            "category": info.get("category", ""),
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
            "search_aliases": list(info.get("search_aliases", [])),
        })
    for nodes in packs.values():
        nodes.sort(key=lambda item: item["name"].lower())
    return web.json_response({"packs": packs})
