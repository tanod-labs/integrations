from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.base import TanodTool
from utils.tanod import source_body


class PdfOcrTool(TanodTool):
    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        body, source = source_body(params, base64_key="pdf_base64")
        body["pages"] = (params.get("pages") or "").strip() or "1-5"
        body["output"] = "url"
        out = self.client().call("/v1/pdf/ocr", body)
        text = out.get("text") or ""
        yield self.create_text_message(text if text.strip() else f"OCR found no text in {source}.")
        f = out.get("file") if isinstance(out.get("file"), dict) else {}
        yield self.create_json_message(
            {
                "source": source,
                "ocr_pages": out.get("ocr_pages"),
                "text_truncated": out.get("text_truncated"),
                "searchable_pdf_url": f.get("url"),
                "searchable_pdf_expires_at": f.get("expires_at"),
                "untrusted_content": True,
            }
        )
