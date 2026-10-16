from collections.abc import Generator
from typing import Any

from dify_plugin.entities.tool import ToolInvokeMessage

from utils.base import TanodTool
from utils.tanod import UNTRUSTED, source_body, to_csv


class BankStatementTool(TanodTool):
    def run(self, params: dict[str, Any]) -> Generator[ToolInvokeMessage]:
        body, source = source_body(params)
        pages = (params.get("pages") or "").strip()
        if pages:
            body["pages"] = pages
        body["format"] = "json"
        out = self.client().call("/v1/pdf/bank-statement", body)
        tx = out.get("transactions") or []
        rows = [[t.get("date"), t.get("description"), t.get("amount"), t.get("balance"), t.get("row_ok")] for t in tx]
        csv_text = to_csv(["date", "description", "amount", "balance", "row_ok"], rows)
        if (params.get("output_format") or "csv") == "json":
            yield self.create_json_message(out)
            yield self.create_text_message(f"{len(tx)} transactions from {source}. {UNTRUSTED}")
            return
        yield self.create_text_message(csv_text)
        yield self.create_json_message({**out, "source": source, "untrusted_content": True, "csv": csv_text})
        yield self.create_blob_message(csv_text.encode(), meta={"mime_type": "text/csv", "filename": "bank-statement.csv"})
