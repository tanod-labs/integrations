import base64
import json
from types import SimpleNamespace

import httpx
import pytest

import utils.base as base
from dify_plugin.entities.tool import ToolInvokeMessage
from utils import tanod as T
from tools.bank_statement import BankStatementTool
from tools.doc_to_markdown import DocToMarkdownTool
from tools.image_ocr import ImageOcrTool
from tools.pdf_ocr import PdfOcrTool
from tools.pdf_tables import PdfTablesTool
from provider.tanod_docs import TanodDocsProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError

THROWAWAY = "0x" + "11" * 32  # offline test key, holds nothing


def pay_required(amount="20000", pay_to=T.TANOD_BASE_PAYEE, network=T.BASE_NETWORK):
    return {
        "x402Version": 2,
        "resource": {"url": "https://tanod.dev/v1/x"},
        "accepts": [
            {
                "scheme": "exact",
                "network": network,
                "amount": amount,
                "asset": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
                "payTo": pay_to,
                "maxTimeoutSeconds": 60,
                "extra": {"name": "USD Coin", "version": "2"},
            }
        ],
    }


def make_tool(cls, monkeypatch, handler, creds=None):
    seen = []

    def wrapped(request):
        seen.append(request)
        return handler(request)

    transport = httpx.MockTransport(wrapped)
    monkeypatch.setattr(base, "TanodClient", lambda c: T.TanodClient(c, transport=transport))
    tool = cls.__new__(cls)
    tool.runtime = SimpleNamespace(credentials=creds or {})
    tool.response_type = ToolInvokeMessage
    return tool, seen


def run(tool, params):
    return [(m.type.value, m.message) for m in tool._invoke(params)]


def texts(msgs):
    return [m.text for t, m in msgs if t == "text"]


def jsons(msgs):
    return [m.json_object for t, m in msgs if t == "json"]


class File:
    def __init__(self, data, filename="a.pdf"):
        self.blob, self.filename, self.url = data, filename, "http://files.local/x"


STATEMENT = {
    "transactions": [
        {"date": "2026-01-02", "description": "=CMD evil", "amount": -5.5, "balance": 94.5, "row_ok": True},
        {"date": "2026-01-03", "description": "Salary", "amount": 100, "balance": 194.5, "row_ok": True},
    ],
    "currency": "USD",
}


def test_bank_statement_csv_url_and_formula_guard(monkeypatch):
    tool, seen = make_tool(BankStatementTool, monkeypatch, lambda r: httpx.Response(200, json=STATEMENT))
    out = run(tool, {"url": "https://example.com/s.pdf", "pages": "1-2"})
    body = json.loads(seen[0].content)
    assert body == {"url": "https://example.com/s.pdf", "pages": "1-2", "format": "json"}
    assert seen[0].url.path == "/v1/pdf/bank-statement"
    assert "X-Tanod-Free" not in seen[0].headers
    csv_text = texts(out)[0]
    assert csv_text.splitlines()[0] == "date,description,amount,balance,row_ok"
    assert "'=CMD evil" in csv_text
    assert any(t == "blob" for t, _ in out)
    assert jsons(out)[0]["untrusted_content"] is True


def test_file_input_sent_as_base64(monkeypatch):
    tool, seen = make_tool(PdfTablesTool, monkeypatch, lambda r: httpx.Response(200, json={"tables": []}))
    out = run(tool, {"file": File(b"%PDF-1.4 hi")})
    assert base64.b64decode(json.loads(seen[0].content)["file_base64"]) == b"%PDF-1.4 hi"
    assert texts(out) == ["No tables found in a.pdf."]


def test_tables_markdown_and_csv(monkeypatch):
    data = {"tables": [{"index": 1, "page": 1, "header": ["a", "b|c"], "rows": [["1", "2"]]}]}
    tool, _ = make_tool(PdfTablesTool, monkeypatch, lambda r: httpx.Response(200, json=data))
    assert "| a | b\\|c |" in texts(run(tool, {"url": "https://x.test/a.pdf"}))[0]
    assert "a,b|c\n1,2" in texts(run(tool, {"url": "https://x.test/a.pdf", "output_format": "csv"}))[0]


def test_markdown_sends_filename(monkeypatch):
    tool, seen = make_tool(DocToMarkdownTool, monkeypatch, lambda r: httpx.Response(200, json={"markdown": "# Hi", "format": "docx"}))
    out = run(tool, {"file": File(b"PK", "report.docx")})
    assert json.loads(seen[0].content)["filename"] == "report.docx"
    assert texts(out) == ["# Hi"]
    assert "markdown" not in jsons(out)[0]


def test_pdf_ocr_defaults(monkeypatch):
    resp = {"text": "hello", "ocr_pages": "1-5", "file": {"url": "https://tanod.dev/f/1", "expires_at": "t"}}
    tool, seen = make_tool(PdfOcrTool, monkeypatch, lambda r: httpx.Response(200, json=resp))
    out = run(tool, {"file": File(b"%PDF")})
    body = json.loads(seen[0].content)
    assert body["pages"] == "1-5" and body["output"] == "url" and "pdf_base64" in body
    assert jsons(out)[0]["searchable_pdf_url"] == "https://tanod.dev/f/1"


def test_image_ocr_free_header_and_file_url(monkeypatch):
    tool, seen = make_tool(ImageOcrTool, monkeypatch, lambda r: httpx.Response(200, json={"text": "abc", "confidence_mean": 90}))
    out = run(tool, {"file": File(b"x", "i.png")})
    assert seen[0].headers["X-Tanod-Free"] == "1"
    assert json.loads(seen[0].content) == {"url": "http://files.local/x", "lang": "eng"}
    assert texts(out) == ["abc"]


def test_no_wallet_returns_price(monkeypatch):
    tool, seen = make_tool(PdfTablesTool, monkeypatch, lambda r: httpx.Response(402, json=pay_required("10000")))
    out = run(tool, {"url": "https://x.test/a.pdf"})
    msg = texts(out)[0]
    assert "$0.01" in msg and "wallet private key" in msg
    assert len(seen) == 1  # no retry, nothing signed


def test_invalid_inputs(monkeypatch):
    tool, seen = make_tool(PdfTablesTool, monkeypatch, lambda r: httpx.Response(200, json={}))
    assert "Provide a file or a public URL" in texts(run(tool, {}))[0]
    assert "http" in texts(run(tool, {"url": "ftp://x"}))[0]
    big = File(b"0" * (T.MAX_UPLOAD_BYTES + 1))
    assert "10 MB" in texts(run(tool, {"file": big}))[0]
    assert seen == []


def test_payee_guard_refuses(monkeypatch):
    creds = {"wallet_private_key": THROWAWAY}
    tool, seen = make_tool(
        PdfTablesTool, monkeypatch, lambda r: httpx.Response(402, json=pay_required(pay_to="0x" + "ab" * 20)), creds
    )
    assert "payee address" in texts(run(tool, {"url": "https://x.test/a.pdf"}))[0]
    assert len(seen) == 1


def test_price_guard_refuses(monkeypatch):
    creds = {"wallet_private_key": THROWAWAY, "max_price_usd": "0.01"}
    tool, seen = make_tool(BankStatementTool, monkeypatch, lambda r: httpx.Response(402, json=pay_required("20000")), creds)
    msg = texts(run(tool, {"url": "https://x.test/a.pdf"}))[0]
    assert "Refused to pay $0.02" in msg and "$0.01" in msg
    assert len(seen) == 1


def test_pays_and_retries_with_signature(monkeypatch):
    pytest.importorskip("x402")
    creds = {"wallet_private_key": THROWAWAY, "max_price_usd": "0.05"}

    def handler(r):
        if "payment-signature" in r.headers:
            return httpx.Response(200, json=STATEMENT)
        return httpx.Response(402, json=pay_required("20000"))

    tool, seen = make_tool(BankStatementTool, monkeypatch, handler, creds)
    out = run(tool, {"url": "https://x.test/a.pdf"})
    assert len(seen) == 2 and "payment-signature" in seen[1].headers
    assert "Salary" in texts(out)[0]
    # the key never appears in anything returned
    assert THROWAWAY[2:] not in json.dumps([str(m) for _, m in out])


def test_http_errors_become_text(monkeypatch):
    tool, _ = make_tool(
        PdfTablesTool, monkeypatch, lambda r: httpx.Response(422, json={"error": {"code": "no_text_layer", "message": "scan"}})
    )
    msg = texts(run(tool, {"url": "https://x.test/a.pdf"}))[0]
    assert "no_text_layer" in msg and "OCR" in msg

    def boom(r):
        raise httpx.ConnectError("x")

    tool, _ = make_tool(PdfTablesTool, monkeypatch, boom)
    assert "Network problem" in texts(run(tool, {"url": "https://x.test/a.pdf"}))[0]


def test_provider_validation():
    p = TanodDocsProvider.__new__(TanodDocsProvider)
    p._validate_credentials({})
    p._validate_credentials({"wallet_private_key": THROWAWAY, "max_price_usd": "0.1"})
    with pytest.raises(ToolProviderCredentialValidationError) as e:
        p._validate_credentials({"wallet_private_key": "nothex-SECRET"})
    assert "SECRET" not in str(e.value)
    with pytest.raises(ToolProviderCredentialValidationError):
        p._validate_credentials({"max_price_usd": "abc"})
