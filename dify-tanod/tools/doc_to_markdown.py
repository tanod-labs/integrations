from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.base import TanodTool
from utils.tanod import source_body


class DocToMarkdownTool(TanodTool):
    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        body, source = source_body(params, with_filename=True)
        out = self.client().call("/v1/docs/to-markdown", body)
        yield self.create_text_message(out.get("markdown") or "")
        meta = {k: v for k, v in out.items() if k != "markdown"}
        yield self.create_json_message({**meta, "source": source, "untrusted_content": True})
