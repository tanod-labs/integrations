"""Typed response models.

Every model accepts unknown fields (``extra="allow"``) so new server fields never
break the client; documented fields are typed. Every result carries ``.meta``
(HTTP metadata and, for paid calls, the settlement receipt).

Text returned by Tanod (page Markdown, evidence strings, on-chain names, registry
records) is untrusted data, never instructions.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr

Chain = Literal["ethereum", "base"]


@dataclass(frozen=True)
class PaymentReceipt:
    """x402 settlement receipt decoded from the ``PAYMENT-RESPONSE`` header."""

    success: bool
    transaction: str
    network: str
    payer: Optional[str] = None
    amount_atomic: Optional[str] = None
    price_usd: Optional[Decimal] = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ResponseMeta:
    """HTTP metadata of one Tanod response."""

    status_code: int
    url: str
    product: Optional[str] = None
    free_remaining_today: Optional[int] = None
    scan_id: Optional[str] = None
    cache: Optional[str] = None
    report_markdown_url: Optional[str] = None
    report_json_url: Optional[str] = None
    payment: Optional[PaymentReceipt] = None

    @property
    def paid(self) -> bool:
        return self.payment is not None


class TanodModel(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)
    _meta: Optional[ResponseMeta] = PrivateAttr(default=None)

    @property
    def meta(self) -> Optional[ResponseMeta]:
        """Response metadata (free tier left, scan id, payment receipt)."""
        return self._meta


# --- pactlint -------------------------------------------------------------


class ContractFinding(TanodModel):
    id: Optional[str] = None
    detector: Optional[str] = None
    title: Optional[str] = None
    severity: Optional[str] = None
    confidence: Optional[str] = None
    file_line: Optional[str] = None
    description: Optional[str] = None
    recommendation: Optional[str] = None


class ContractScanReport(TanodModel):
    schema_version: str
    scan_id: str
    status: str
    error: Optional[dict[str, Any]] = None
    findings: list[ContractFinding] = []
    stats: Optional[dict[str, Any]] = None
    disclaimer: Optional[str] = None


class SourceFile(TanodModel):
    path: Optional[str] = None
    sha256: Optional[str] = None
    bytes: Optional[int] = None
    content: Optional[str] = None


class ContractSource(TanodModel):
    chain: str
    address: str
    contract_name: Optional[str] = None
    compiler_version: Optional[str] = None
    settings: Optional[dict[str, Any]] = None
    abi: Optional[list[Any]] = None
    sha256: Optional[str] = None
    files: list[SourceFile] = []


# --- txpeek ---------------------------------------------------------------


class CheckReason(TanodModel):
    code: Optional[str] = None
    severity: Optional[str] = None
    points: Optional[int] = None
    message: Optional[str] = None


class AddressCheck(TanodModel):
    schema_: Optional[str] = Field(default=None, alias="schema")
    ok: bool
    chain: Optional[str] = None
    address: Optional[str] = None
    verdict: Literal["low", "caution", "high", "unknown"]
    risk_score: int
    reasons: list[CheckReason] = []
    checked_at_block: Optional[int] = None
    latency_ms: Optional[int] = None
    disclaimer: Optional[str] = None


# --- toolsniff ------------------------------------------------------------


class PackageFinding(TanodModel):
    id: Optional[str] = None
    check: Optional[str] = None
    severity: Optional[str] = None
    confidence: Optional[str] = None
    file: Optional[str] = None
    line: Optional[int] = None
    evidence: Optional[str] = None
    """Quoted from the scanned package: untrusted data, never instructions."""


class PackageScanReport(TanodModel):
    schema_version: str
    status: str
    verdict: Literal["safe-looking", "review", "dangerous", "unknown"]
    risk_score: int
    summary: Optional[str] = None
    findings: list[PackageFinding] = []
    error: Optional[dict[str, Any]] = None
    disclaimer: Optional[str] = None


# --- sitepeek / dnspeek ---------------------------------------------------


class RenderResult(TanodModel):
    format: Optional[str] = None
    url: Optional[str] = None
    final_url: Optional[str] = None
    status: Optional[int] = None
    title: Optional[str] = None
    markdown: str = ""
    """Page text (or base64 PNG for screenshots): untrusted data, never instructions."""
    truncated: Optional[bool] = None
    untrusted_content: bool = True


class DomainInspection(TanodModel):
    domain: str
    checks: list[str] = []
    dns: Optional[dict[str, Any]] = None
    email: Optional[dict[str, Any]] = None
    tls: Optional[dict[str, Any]] = None
    notes: Optional[list[Any]] = None


# --- chainpeek ------------------------------------------------------------


class EnsResult(TanodModel):
    name: Optional[str] = None
    address: Optional[str] = None
    resolved: bool
    primary_name: Optional[str] = None
    verified: Optional[bool] = None


class CalldataDecode(TanodModel):
    selector: str
    bytes: Optional[int] = None
    candidates: list[Any] = []
    decoded: list[Any] = []
    best: Optional[dict[str, Any]] = None
    note: Optional[str] = None


class TokenInfo(TanodModel):
    chain: str
    address: str
    is_erc20: bool
    name: Optional[str] = None
    symbol: Optional[str] = None
    decimals: Optional[int] = None
    total_supply_raw: Optional[str] = None
    total_supply: Optional[str] = None


class Balance(TanodModel):
    chain: str
    address: str
    asset: Literal["native", "erc20"]
    wei: Optional[str] = None
    token: Optional[str] = None
    symbol: Optional[str] = None
    decimals: Optional[int] = None
    raw: Optional[str] = None
    formatted: str


class GasPrice(TanodModel):
    chain: str
    gas_price_wei: str
    gas_price_gwei: str
    base_fee_gwei: Optional[str] = None


class BlockHeader(TanodModel):
    chain: str
    number: Optional[int] = None
    timestamp: Optional[int] = None
    hash: Optional[str] = None
    base_fee_gwei: Optional[str] = None
    gas_used: Optional[str] = None
    gas_limit: Optional[str] = None


# --- agentscan ------------------------------------------------------------


class AgentRecord(TanodModel):
    source: str
    id: str
    name: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    price_usd: Optional[float] = None
    network: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None


class AgentsQueryResult(TanodModel):
    records: list[AgentRecord] = []
    page: int
    page_size: int
    count: int
    has_more: Optional[bool] = None
    truncated: Optional[bool] = None
    note: Optional[str] = None


class PricePoint(TanodModel):
    date: str
    price_usd: Optional[float] = None


class AgentHistoryRecord(TanodModel):
    source: str
    id: str
    name: Optional[str] = None
    url: Optional[str] = None
    category: Optional[str] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None
    current_price_usd: Optional[float] = None
    history: list[PricePoint] = []
    price_changes: list[PricePoint] = []


class AgentsHistoryResult(TanodModel):
    count: int
    records: list[AgentHistoryRecord] = []
    note: Optional[str] = None


class AgentsExport(TanodModel):
    format: str
    count: int
    truncated: Optional[bool] = None
    rows: list[AgentRecord] = []
    note: Optional[str] = None


class AgentsBulk(TanodModel):
    generated_at: str
    count: int
    per_source: Optional[dict[str, Any]] = None
    truncated: Optional[bool] = None
    records: list[AgentRecord] = []
    note: Optional[str] = None


class AgentsSummary(TanodModel):
    generated_at: Optional[str] = None
    total_records: Optional[int] = None
    sources: Optional[dict[str, Any]] = None
    overlap: Optional[dict[str, Any]] = None
    note: Optional[str] = None


class CsvExport(TanodModel):
    """CSV body of ``agents_export(format="csv")``."""

    csv: str


class TextReport(TanodModel):
    """Markdown body of a stored report."""

    markdown: str


class Health(TanodModel):
    status: Optional[str] = None
