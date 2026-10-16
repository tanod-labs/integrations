from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.base import TanodTool
from utils.tanod import TanodError


class ImageOcrTool(TanodTool):
    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        url = (params.get("url") or "").strip()
        file = params.get("file")
        if not url and file is not None:
            url = (getattr(file, "url", "") or "").strip()  # the API fetches by URL only
        if not url:
            raise TanodError("Provide a public image URL, or a file whose Dify URL is publicly reachable.")
        if not url.lower().startswith(("http://", "https://")):
            raise TanodError("The image URL must start with http:// or https://")
        lang = (params.get("lang") or "eng").strip() or "eng"
        out = self.client().call("/v1/ocr", {"url": url, "lang": lang}, free=True)
        yield self.create_text_message(out.get("text") or "")
        yield self.create_json_message(
            {
                "source": url,
                "text_truncated": out.get("truncated"),
                "confidence_mean": out.get("confidence_mean"),
                "words": out.get("words"),
                "untrusted_content": True,
            }
        )
