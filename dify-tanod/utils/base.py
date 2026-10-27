"""Shared behaviour for the five Tanod tools.

A mixin, not a Tool subclass: the Dify SDK loads a tool module only if it holds exactly one Tool subclass
(dify_plugin.core.utils.class_loader.get_subclasses_from_module), so an imported shared Tool base would count twice.
"""

from __future__ import annotations

from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.tanod import TanodClient, TanodError


class TanodTool:
    """Mix in before dify_plugin.Tool: `class X(TanodTool, Tool)`. Subclasses implement ``run`` and yield messages; errors become one readable text message."""

    def client(self) -> TanodClient:
        return TanodClient(dict(self.runtime.credentials or {}))

    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:  # pragma: no cover
        raise NotImplementedError

    def _invoke(self, tool_parameters: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        try:
            yield from self.run(tool_parameters)
        except TanodError as e:
            yield self.create_text_message(f"Tanod error: {e}")
            yield self.create_json_message({"error": str(e)})
