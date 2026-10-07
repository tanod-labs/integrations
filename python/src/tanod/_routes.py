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
_TX_HASH = re.compile(r"^0x[0-9a-fA-F]{64}$")
_OCR_LANG = re.compile(r"^[a-z][a-z_]{1,15}(\+[a-z][a-z_]{1,15}){0,2}$")
_PAIRS = ("ETH/USD", "BTC/USD", "USDC/USD", "USDT/USD", "DAI/USD", "LINK/USD", "stETH/USD", "cbETH/USD", "cbETH/ETH")
_FRESHNESS = ("day", "week", "month", "year")


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


def extract_pdf(url: str, max_pages: Optional[int]) -> Route:
    if max_pages is not None and not 1 <= max_pages <= 200:
        raise InvalidRequestError("max_pages must be between 1 and 200")
    return Route("POST", "/v1/pdf", m.PdfResult, _drop_none({"url": url, "max_pages": max_pages}))


def page_meta(url: str) -> Route:
    return Route("POST", "/v1/meta", m.PageMeta, {"url": url})


def ocr_image(url: str, lang: Optional[str]) -> Route:
    if lang is not None and not _OCR_LANG.match(lang):
        raise InvalidRequestError("lang must be a Tesseract language code such as 'eng' (or 'eng+deu')")
    return Route("POST", "/v1/ocr", m.OcrResult, _drop_none({"url": url, "lang": lang}))


def rdap_lookup(query: str) -> Route:
    if not isinstance(query, str) or not 1 <= len(query) <= 255:
        raise InvalidRequestError("query must be a domain, an IP address or an AS number (1-255 characters)")
    return Route("POST", "/v1/rdap", m.RdapResult, {"query": query})


def verify_email(email: str) -> Route:
    if not isinstance(email, str) or not 1 <= len(email) <= 320:
        raise InvalidRequestError("email must be 1-320 characters")
    return Route("POST", "/v1/email/verify", m.EmailVerification, {"email": email})


def ip_lookup(ip: str) -> Route:
    if not isinstance(ip, str) or not 1 <= len(ip) <= 64:
        raise InvalidRequestError("ip must be a single IPv4 or IPv6 address")
    return Route("POST", "/v1/ip", m.IpLookup, {"ip": ip})


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


def token_price(chain: str, pair: str) -> Route:
    if pair not in _PAIRS:
        raise InvalidRequestError(f"pair must be one of {', '.join(_PAIRS)}, got {pair!r}")
    return Route("POST", "/v1/chain/price", m.TokenPrice, {"chain": _chain(chain), "pair": pair})


def transaction(chain: str, hash: str) -> Route:
    if not isinstance(hash, str) or not _TX_HASH.match(hash):
        raise InvalidRequestError(f"hash must be 0x followed by 64 hex characters, got {hash!r}")
    return Route("POST", "/v1/chain/tx", m.Transaction, {"chain": _chain(chain), "hash": hash})


def nft(chain: str, contract: str, token_id: str) -> Route:
    if isinstance(token_id, int) and not isinstance(token_id, bool) and token_id >= 0:
        token_id = str(token_id)
    if not isinstance(token_id, str) or not 1 <= len(token_id) <= 80:
        raise InvalidRequestError("token_id must be a uint256 as a decimal or 0x-hex string")
    body = {"chain": _chain(chain), "contract": _addr(contract, "contract"), "token_id": token_id}
    return Route("POST", "/v1/chain/nft", m.NftInfo, body)


def allowance(chain: str, token: str, owner: str, spender: str) -> Route:
    body = {
        "chain": _chain(chain),
        "token": _addr(token, "token"),
        "owner": _addr(owner, "owner"),
        "spender": _addr(spender, "spender"),
    }
    return Route("POST", "/v1/chain/allowance", m.Allowance, body)


def portfolio(chain: str, address: str, tokens: Optional[Sequence[str]]) -> Route:
    toks = None
    if tokens is not None:
        toks = [_addr(t, "token") for t in tokens]
        if len(toks) > 20:
            raise InvalidRequestError("tokens takes at most 20 addresses")
    body = _drop_none({"chain": _chain(chain), "address": _addr(address), "tokens": toks})
    return Route("POST", "/v1/chain/portfolio", m.Portfolio, body)


def swap_quote(
    chain: str,
    token_in: str,
    token_out: str,
    amount_in: Optional[str],
    amount_in_raw: Optional[str],
) -> Route:
    if (amount_in is None) == (amount_in_raw is None):
        raise InvalidRequestError("give exactly one of amount_in or amount_in_raw")
    body = _drop_none(
        {
            "chain": _chain(chain),
            "token_in": _addr(token_in, "token_in"),
            "token_out": _addr(token_out, "token_out"),
            "amount_in": None if amount_in is None else str(amount_in),
            "amount_in_raw": None if amount_in_raw is None else str(amount_in_raw),
        }
    )
    return Route("POST", "/v1/chain/quote", m.SwapQuote, body)


# --- findpeek / weatherpeek -----------------------------------------------


def web_search(
    query: str, count: Optional[int], country: Optional[str], freshness: Optional[str]
) -> Route:
    if not isinstance(query, str) or not 1 <= len(query) <= 512:
        raise InvalidRequestError("query must be 1-512 characters")
    if count is not None and not 1 <= count <= 10:
        raise InvalidRequestError("count must be between 1 and 10")
    if country is not None and (not isinstance(country, str) or len(country) != 2):
        raise InvalidRequestError("country must be a two-letter ISO 3166-1 code")
    if freshness is not None and freshness not in _FRESHNESS:
        raise InvalidRequestError("freshness must be one of day, week, month, year")
    body = _drop_none({"query": query, "count": count, "country": country, "freshness": freshness})
    return Route("POST", "/v1/search", m.WebSearchResult, body)


def weather(
    lat: Optional[float], lon: Optional[float], place: Optional[str], hours: Optional[int]
) -> Route:
    coords = lat is not None or lon is not None
    if coords == (place is not None) or (coords and (lat is None or lon is None)):
        raise InvalidRequestError("give lat and lon together, or place alone")
    if lat is not None and not -90 <= lat <= 90:
        raise InvalidRequestError("lat must be between -90 and 90")
    if lon is not None and not -180 <= lon <= 180:
        raise InvalidRequestError("lon must be between -180 and 180")
    if place is not None and not 1 <= len(place) <= 120:
        raise InvalidRequestError("place must be 1-120 characters")
    if hours is not None and not 1 <= hours <= 48:
        raise InvalidRequestError("hours must be between 1 and 48")
    body = _drop_none({"lat": lat, "lon": lon, "place": place, "hours": hours})
    return Route("POST", "/v1/weather", m.WeatherForecast, body)


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
