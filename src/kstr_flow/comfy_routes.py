from __future__ import annotations

from aiohttp import web
from server import PromptServer

from .analyze import analyze_source
from .registry import get_runtime_registry
from .naming import str_to_class_id


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
        info = meta.info or {}
        input_info = info.get("input") or {}
        inputs = []
        for group in ("required", "optional"):
            group_inputs = input_info.get(group) or {}
            for name, value in group_inputs.items():
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
        output_types = info.get("output") or []
        output_names = info.get("output_name") or output_types
        aliases = info.get("search_aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        else:
            try:
                aliases = list(aliases)
            except TypeError:
                aliases = []

        packs.setdefault(meta.namespace, []).append({
            "name": raw_name,
            "call_name": str_to_class_id(raw_name),
            "display_name": info.get("display_name") or raw_name,
            "description": info.get("description") or "",
            "category": info.get("category") or "",
            "inputs": inputs,
            "outputs": [
                {"name": name, "type": typ}
                for name, typ in zip(output_names, output_types)
            ],
            "deprecated": bool(info.get("deprecated", False)),
            "experimental": bool(info.get("experimental", False)),
            "search_aliases": aliases,
        })
    for nodes in packs.values():
        nodes.sort(key=lambda item: item["name"].lower())
    return web.json_response({"packs": packs})
