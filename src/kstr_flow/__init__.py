"""KSTR Flow: a Pythonic workflow language embedded in ComfyUI.

The project reuses ComfyScript's node metadata/runtime machinery while adding a
safe evaluator, package namespaces, deterministic seed helpers, and ComfyUI
node-expansion integration.
"""

from .flow import FlowProgram, compile_flow
from .registry import FlowRegistry

__all__ = ["FlowProgram", "FlowRegistry", "compile_flow"]
