from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.base import TanodTool
from utils.tanod import source_body, to_csv


def _md(header, rows) -> str:
    cols = max([len(header or [])] + [len(r) for r in rows] + [1])

    def line(cells):
        cells = [str(c).replace("|", "\\|").replace("\n", " ") for c in cells]
        return "| " + " | ".join(cells + [""] * (cols - len(cells))) + " |"

    head = header or [f"col{i + 1}" for i in range(cols)]
    return "\n".join([line(head), "| " + " | ".join(["---"] * cols) + " |"] + [line(r) for r in rows])


class PdfTablesTool(TanodTool):
    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        body, source = source_body(params)
        pages = (params.get("pages") or "").strip()
        if pages:
            body["pages"] = pages
        body["format"] = "json"
        out = self.client().call("/v1/pdf/tables", body)
        tables = out.get("tables") or []
        if not tables:
            yield self.create_text_message(f"No tables found in {source}.")
            yield self.create_json_message({**out, "source": source})
            return
        as_csv = (params.get("output_format") or "markdown") == "csv"
        parts = []
        for t in tables:
            rows = t.get("rows") or []
            body_text = to_csv(t.get("header"), rows) if as_csv else _md(t.get("header"), rows)
            parts.append(f"Table {t.get('index')} (page {t.get('page')}, {len(rows)} rows)\n{body_text}")
        yield self.create_text_message("\n\n".join(parts))
        yield self.create_json_message({**out, "source": source, "untrusted_content": True})
