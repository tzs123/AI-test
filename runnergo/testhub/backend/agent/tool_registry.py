from __future__ import annotations

from .core.tool_registry import Tool, ToolRegistry, default_registry


def tool_catalog() -> list[dict]:
    return default_registry.list()


__all__ = ['Tool', 'ToolRegistry', 'default_registry', 'tool_catalog']
