from __future__ import annotations

import asyncio

from aiohttp import web
from server import PromptServer

from .analyze import analyze_source
from .preview import build_preview_graph
from .registry import get_runtime_registry, input_combo_values
from .naming import str_to_class_id


@PromptServer.instance.routes.post("/kstr-flow/analyze")
async def kstr_flow_analyze(request):
    payload = await request.json()
    source = payload.get("source", "")
    if not isinstance(source, str):
        return web.json_response({"ok": False, "error": {"message": "source must be a string"}}, status=400)
    # Keep editor diagnostics/socket inference fast and independent from graph
    # preview execution. The preview has its own bounded endpoint below.
    return web.json_response(
        analyze_source(source, get_runtime_registry(), include_preview=False)
    )


@PromptServer.instance.routes.post("/kstr-flow/preview")
async def kstr_flow_preview(request):
    payload = await request.json()
    source = payload.get("source", "")
    if not isinstance(source, str):
        return web.json_response({"ok": False, "error": "source must be a string"}, status=400)

    registry = get_runtime_registry()
    try:
        # A malformed/custom wrapper must never block Comfy's aiohttp event loop.
        # Even if a worker thread gets stuck, the request itself has a hard bound.
        graph = await asyncio.wait_for(
            asyncio.to_thread(build_preview_graph, source, registry),
            timeout=5.0,
        )
        return web.json_response({"ok": True, "graph": graph, "graph_error": None})
    except (asyncio.TimeoutError, TimeoutError):
        return web.json_response(
            {"ok": False, "graph": None, "graph_error": "Preview compilation timed out after 5s"},
            status=504,
        )
    except Exception as exc:
        return web.json_response({
            "ok": False,
            "graph": None,
            "graph_error": f"{type(exc).__name__}: {exc}",
        })


@PromptServer.instance.routes.get("/kstr-flow/options")
async def kstr_flow_options(request):
    pack = request.query.get("pack", "")
    node_name = request.query.get("node", "")
    input_name = request.query.get("input", "")
    if not pack or not node_name or not input_name:
        return web.json_response({"options": []}, status=400)
    values = input_combo_values(get_runtime_registry(), pack, node_name, input_name)
    return web.json_response({"options": values})


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
