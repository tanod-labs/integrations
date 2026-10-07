"""Request specs for every Tanod route (one place, shared by sync and async clients).

Light client-side validation catches obvious mistakes before any network call;
the server stays the authority (an invalid input is never charged either way).
"""

from __future__ import annotations

import base64
import re
from dataclasses import dataclass
from typing import Any, Optional, Sequence

from . import _models as m
from ._errors import InvalidRequestError

_ADDR = re.compile(r"^0x[0-9a-fA-F]{40}$")
_CHAINS = ("ethereum", "base")


@dataclass(frozen=True)
class Route:
    method: str
    path: str
    model: type[m.TanodModel]
    json: Optional[dict[str, Any]] = None
    kind: str = "json"  # json | csv | markdown


def _addr(value: str, what: str = "address") -> str:
    if not isinstance(value, str) or not _ADDR.match(value):
        raise InvalidRequestError(f"{what} must be 0x followed by 40 hex characters, got {value!r}")
    return value


def _chain(value: str) -> str:
    if value not in _CHAINS:
        raise InvalidRequestError(f"chain must be 'ethereum' or 'base', got {value!r}")
    return value


def _drop_none(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _scan_options(
    include_informational: Optional[bool],
    include_noisy: Optional[bool],
    include_dependencies: Optional[bool],
) -> Optional[dict[str, bool]]:
    opts = _drop_none(
        {
            "include_informational": include_informational,
            "include_noisy": include_noisy,
            "include_dependencies": include_dependencies,
        }
    )
    return opts or None


# --- pactlint -------------------------------------------------------------


def scan_contract_source(
    source: Optional[str],
    standard_json: Optional[dict[str, Any]],
    filename: Optional[str],
    compiler_version: Optional[str],
    include_informational: Optional[bool],
    include_noisy: Optional[bool],
    include_dependencies: Optional[bool],
) -> Route:
    if (source is None) == (standard_json is None):
        raise InvalidRequestError("give exactly one of source or standard_json")
    body = _drop_none(
        {
            "source": source,
            "standard_json": standard_json,
            "filename": filename,
            "compiler_version": compiler_version,
            "options": _scan_options(include_informational, include_noisy, include_dependencies),
        }
    )
    return Route("POST", "/v1/scan/source", m.ContractScanReport, body)


def scan_contract_address(
    address: str,
    chain: str,
    include_informational: Optional[bool],
    include_noisy: Optional[bool],
    include_dependencies: Optional[bool],
) -> Route:
    body = _drop_none(
        {
            "address": _addr(address),
            "chain": _chain(chain),
            "options": _scan_options(include_informational, include_noisy, include_dependencies),
        }
    )
    return Route("POST", "/v1/scan/address", m.ContractScanReport, body)


def get_contract_source(chain: str, address: str) -> Route:
    return Route("GET", f"/v1/source/{_chain(chain)}/{_addr(address)}", m.ContractSource)


# --- txpeek ---------------------------------------------------------------


def check_address(address: str, chain: str) -> Route:
    body = {"address": _addr(address), "chain": _chain(chain)}
    return Route("POST", "/v1/check/address", m.AddressCheck, body)


# --- toolsniff ------------------------------------------------------------


def scan_package(
    source: Optional[str], content: Optional[bytes], filename: Optional[str]
) -> Route:
    if (source is None) == (content is None):
        raise InvalidRequestError("give exactly one of source or content")
    if content is not None:
        body = _drop_none(
            {"content_base64": base64.b64encode(content).decode("ascii"), "filename": filename}
        )
    else:
        body = {"source": source}
    return Route("POST", "/v1/scan/package", m.PackageScanReport, body)


# --- sitepeek / dnspeek ---------------------------------------------------


def render(
    url: str, format: str, js: bool, width: Optional[int], height: Optional[int]
) -> Route:
    if format not in ("markdown", "screenshot"):
        raise InvalidRequestError("format must be 'markdown' or 'screenshot'")
    body = _drop_none(
        {"url": url, "format": format, "js": js or None, "width": width, "height": height}
    )
    return Route("POST", "/v1/render", m.RenderResult, body)


def inspect_domain(domain: str, checks: Optional[Sequence[str]]) -> Route:
    if checks is not None:
        checks = list(checks)
        bad = [c for c in checks if c not in ("dns", "email", "tls")]
        if bad or not checks:
            raise InvalidRequestError("checks must be a non-empty subset of dns, email, tls")
    body = _drop_none({"domain": domain, "checks": checks})
    return Route("POST", "/v1/domain/inspect", m.DomainInspection, body)


# --- chainpeek ------------------------------------------------------------


def resolve_ens(name: Optional[str], address: Optional[str]) -> Route:
    if (name is None) == (address is None):
        raise InvalidRequestError("give exactly one of name or address")
    body = _drop_none({"name": name, "address": address})
    return Route("POST", "/v1/chain/ens", m.EnsResult, body)


def decode_calldata(calldata: str, signature: Optional[str]) -> Route:
    if not isinstance(calldata, str) or not calldata.startswith("0x") or len(calldata) < 10:
        raise InvalidRequestError("calldata must be 0x-prefixed hex with at least a 4-byte selector")
    body = _drop_none({"calldata": calldata, "signature": signature})
    return Route("POST", "/v1/chain/calldata", m.CalldataDecode, body)


def token_info(chain: str, address: str) -> Route:
    body = {"chain": _chain(chain), "address": _addr(address)}
    return Route("POST", "/v1/chain/token", m.TokenInfo, body)


def balance(chain: str, address: str, token: Optional[str]) -> Route:
    body = _drop_none(
        {
            "chain": _chain(chain),
            "address": _addr(address),
            "token": _addr(token, "token") if token is not None else None,
        }
    )
    return Route("POST", "/v1/chain/balance", m.Balance, body)


def gas_price(chain: str) -> Route:
    return Route("POST", "/v1/chain/gas", m.GasPrice, {"chain": _chain(chain)})


def latest_block(chain: str) -> Route:
    return Route("POST", "/v1/chain/block", m.BlockHeader, {"chain": _chain(chain)})


# --- agentscan ------------------------------------------------------------


def agents_summary() -> Route:
    return Route("GET", "/v1/agents/summary", m.AgentsSummary)


def agents_query(
    source: Optional[str],
    category: Optional[str],
    network: Optional[str],
    min_price_usd: Optional[float],
    max_price_usd: Optional[float],
    q: Optional[str],
    page: Optional[int],
    page_size: Optional[int],
) -> Route:
    if page_size is not None and not 1 <= page_size <= 100:
        raise InvalidRequestError("page_size must be between 1 and 100")
    body = _drop_none(
        {
            "source": source,
            "category": category,
            "network": network,
            "min_price_usd": min_price_usd,
            "max_price_usd": max_price_usd,
            "q": q,
            "page": page,
            "page_size": page_size,
        }
    )
    return Route("POST", "/v1/agents/query", m.AgentsQueryResult, body)


def agents_history(source: Optional[str], id: Optional[str], url: Optional[str]) -> Route:
    if url is None and (source is None or id is None):
        raise InvalidRequestError("give source and id together, or url")
    body = _drop_none({"source": source, "id": id, "url": url})
    return Route("POST", "/v1/agents/history", m.AgentsHistoryResult, body)


def agents_export(
    source: Optional[str],
    category: Optional[str],
    network: Optional[str],
    format: str,
    limit: Optional[int],
) -> Route:
    if format not in ("json", "csv"):
        raise InvalidRequestError("format must be 'json' or 'csv'")
    body = _drop_none(
        {"source": source, "category": category, "network": network, "format": format, "limit": limit}
    )
    if format == "csv":
        return Route("POST", "/v1/agents/export", m.CsvExport, body, kind="csv")
    return Route("POST", "/v1/agents/export", m.AgentsExport, body)


def agents_bulk(source: Optional[str]) -> Route:
    return Route("POST", "/v1/agents/bulk", m.AgentsBulk, _drop_none({"source": source}))


# --- shared ---------------------------------------------------------------

_SCAN_ID = re.compile(r"^[A-Za-z0-9_\-]{1,128}$")


def get_report(scan_id: str, format: str) -> Route:
    if not _SCAN_ID.match(scan_id or ""):
        raise InvalidRequestError("scan_id must be alphanumeric (with - or _)")
    if format == "markdown":
        return Route("GET", f"/v1/report/{scan_id}.md", m.TextReport, kind="markdown")
    if format == "json":
        return Route("GET", f"/v1/report/{scan_id}.json", m.TanodModel)
    raise InvalidRequestError("format must be 'json' or 'markdown'")


def health() -> Route:
    return Route("GET", "/healthz", m.Health)
