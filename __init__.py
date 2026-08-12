from __future__ import annotations

import sys
from pathlib import Path

if sys.version_info < (3, 10):
    raise RuntimeError("KSTR Flow requires Python 3.10+")

root = Path(__file__).resolve().parent
src = root / "src"
if str(src) not in sys.path:
    sys.path.insert(0, str(src))

WEB_DIRECTORY = "./src/kstr_flow/web"

# Pytest/build tools import the repository package outside ComfyUI. Register the
# actual extension only when ComfyUI's server/runtime modules are present.
try:
    import server as _comfy_server  # noqa: F401,E402
    import comfy_api as _comfy_api  # noqa: F401,E402
except ModuleNotFoundError:
    __all__ = ["WEB_DIRECTORY"]
else:
    from kstr_flow.comfy_node import KSTRFlowExtension, comfy_entrypoint  # noqa: E402
    __all__ = ["KSTRFlowExtension", "WEB_DIRECTORY", "comfy_entrypoint"]
