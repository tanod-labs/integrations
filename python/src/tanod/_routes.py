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


# --- skypeek --------------------------------------------------------------


def _stations(stations: Sequence[str]) -> list[str]:
    if isinstance(stations, str) or not 1 <= len(stations) <= 20:
        raise InvalidRequestError("stations must be a list of 1-20 ICAO identifiers")
    if any(not isinstance(x, str) or not 1 <= len(x) <= 16 for x in stations):
        raise InvalidRequestError("each station must be a string of 1-16 characters")
    return list(stations)


def _latlon(lat: float, lon: float) -> None:
    if not -90 <= lat <= 90:
        raise InvalidRequestError("lat must be between -90 and 90")
    if not -180 <= lon <= 180:
        raise InvalidRequestError("lon must be between -180 and 180")


def _text(value: Any, what: str, max_len: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= max_len:
        raise InvalidRequestError(f"{what} must be 1-{max_len} characters")
    return value


def _strings(values: Sequence[str], what: str, max_items: int, max_len: int) -> list[str]:
    if isinstance(values, str) or not 1 <= len(values) <= max_items:
        raise InvalidRequestError(f"{what} must be a list of 1-{max_items} strings")
    return [_text(v, f"each of {what}", max_len) for v in values]


def aviation_metar(stations: Sequence[str], decode: Optional[bool]) -> Route:
    return Route("POST", "/v1/aviation/metar", m.MetarReport, _drop_none({"stations": _stations(stations), "decode": decode}))


def aviation_taf(stations: Sequence[str], decode: Optional[bool]) -> Route:
    return Route("POST", "/v1/aviation/taf", m.MetarReport, _drop_none({"stations": _stations(stations), "decode": decode}))


def decode_report(raw: str, kind: Optional[str]) -> Route:
    if kind is not None and kind not in ("auto", "metar", "taf"):
        raise InvalidRequestError("kind must be 'auto', 'metar' or 'taf'")
    return Route("POST", "/v1/aviation/metar/decode", m.ReportDecode, _drop_none({"raw": _text(raw, "raw", 2000), "kind": kind}))


def airport_lookup(code: Optional[str], query: Optional[str], limit: Optional[int], country: Optional[str]) -> Route:
    if (code is None) == (query is None):
        raise InvalidRequestError("give code or query, not both")
    if code is not None and not 2 <= len(code) <= 8:
        raise InvalidRequestError("code must be 2-8 characters")
    if query is not None and not 2 <= len(query) <= 100:
        raise InvalidRequestError("query must be 2-100 characters")
    if limit is not None and not 1 <= limit <= 20:
        raise InvalidRequestError("limit must be between 1 and 20")
    if country is not None and len(country) != 2:
        raise InvalidRequestError("country must be a two-letter ISO 3166-1 code")
    return Route("POST", "/v1/aviation/airport", m.AirportResult,
                 _drop_none({"code": code, "query": query, "limit": limit, "country": country}))


def _point(value: Any, what: str) -> Any:
    if isinstance(value, str) and value:
        return value
    if isinstance(value, dict) and set(value) == {"lat", "lon"}:
        _latlon(value["lat"], value["lon"])
        return value
    raise InvalidRequestError(f"{what} must be an airport code or {{'lat': .., 'lon': ..}}")


def airport_distance(origin: Any, destination: Any) -> Route:
    return Route("POST", "/v1/aviation/distance", m.AirportDistance,
                 {"from": _point(origin, "origin"), "to": _point(destination, "destination")})


def space_weather() -> Route:
    return Route("POST", "/v1/space/weather", m.SpaceWeather, {})


def aurora(lat: float, lon: float) -> Route:
    _latlon(lat, lon)
    return Route("POST", "/v1/space/aurora", m.AuroraNowcast, {"lat": lat, "lon": lon})


def asteroids(days: Optional[int], dist_max_au: Optional[float]) -> Route:
    if days is not None and not 1 <= days <= 30:
        raise InvalidRequestError("days must be between 1 and 30")
    if dist_max_au is not None and not 0.0001 <= dist_max_au <= 0.2:
        raise InvalidRequestError("dist_max_au must be between 0.0001 and 0.2")
    return Route("POST", "/v1/space/asteroids", m.AsteroidApproaches,
                 _drop_none({"days": days, "dist_max_au": dist_max_au}))


_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def sun_moon(lat: float, lon: float, date: Optional[str], tz: Optional[str]) -> Route:
    _latlon(lat, lon)
    if date is not None and not _DATE.match(date):
        raise InvalidRequestError("date must be YYYY-MM-DD")
    if tz is not None:
        _text(tz, "tz", 64)
    return Route("POST", "/v1/space/sun-moon", m.SunMoon, _drop_none({"lat": lat, "lon": lon, "date": date, "tz": tz}))


def satellite_passes(
    lat: float,
    lon: float,
    alt_m: Optional[float],
    norad_id: Optional[int],
    days: Optional[int],
    min_elevation: Optional[float],
    visible_only: Optional[bool],
) -> Route:
    _latlon(lat, lon)
    if alt_m is not None and not -500 <= alt_m <= 9000:
        raise InvalidRequestError("alt_m must be between -500 and 9000")
    if norad_id is not None and not 1 <= norad_id <= 999999999:
        raise InvalidRequestError("norad_id must be a positive integer")
    if days is not None and not 1 <= days <= 3:
        raise InvalidRequestError("days must be between 1 and 3")
    if min_elevation is not None and not 0 <= min_elevation <= 89:
        raise InvalidRequestError("min_elevation must be between 0 and 89")
    body = _drop_none({"lat": lat, "lon": lon, "alt_m": alt_m, "norad_id": norad_id, "days": days,
                       "min_elevation": min_elevation, "visible_only": visible_only})
    return Route("POST", "/v1/space/passes", m.SatellitePasses, body)


# --- mlpeek ---------------------------------------------------------------

_ML_MODELS = ("small-en", "multilingual")


def _ml_model(model: Optional[str]) -> None:
    if model is not None and model not in _ML_MODELS:
        raise InvalidRequestError("model must be 'small-en' or 'multilingual'")


def embed(
    texts: Sequence[str], model: Optional[str], normalize: Optional[bool], encoding: Optional[str], input_type: Optional[str]
) -> Route:
    _ml_model(model)
    if encoding is not None and encoding not in ("float", "base64"):
        raise InvalidRequestError("encoding must be 'float' or 'base64'")
    if input_type is not None and input_type not in ("none", "query", "passage"):
        raise InvalidRequestError("input_type must be 'none', 'query' or 'passage'")
    body = _drop_none({"texts": _strings(texts, "texts", 64, 8000), "model": model, "normalize": normalize,
                       "encoding": encoding, "input_type": input_type})
    return Route("POST", "/v1/embed", m.EmbedResult, body)


def rerank(query: str, documents: Sequence[str], top_k: Optional[int]) -> Route:
    if top_k is not None and not 1 <= top_k <= 100:
        raise InvalidRequestError("top_k must be between 1 and 100")
    body = _drop_none({"query": _text(query, "query", 2000), "documents": _strings(documents, "documents", 100, 4000), "top_k": top_k})
    return Route("POST", "/v1/rerank", m.RerankResult, body)


def similarity(a: Optional[str], b: Optional[str], pairs: Optional[Sequence[Any]], model: Optional[str]) -> Route:
    _ml_model(model)
    single = a is not None or b is not None
    if single == (pairs is not None) or (single and (a is None or b is None)):
        raise InvalidRequestError("give a and b together, or pairs")
    body: dict[str, Any] = {}
    if single:
        body = {"a": _text(a, "a", 4000), "b": _text(b, "b", 4000)}
    else:
        assert pairs is not None
        if isinstance(pairs, str) or not 1 <= len(pairs) <= 50:
            raise InvalidRequestError("pairs must be a list of 1-50 (a, b) pairs")
        out = []
        for p in pairs:
            if isinstance(p, dict):
                pa, pb = p.get("a"), p.get("b")
            else:
                try:
                    pa, pb = p
                except (TypeError, ValueError):
                    raise InvalidRequestError("each pair must be (a, b) or {'a': .., 'b': ..}") from None
            out.append({"a": _text(pa, "pair a", 4000), "b": _text(pb, "pair b", 4000)})
        body = {"pairs": out}
    if model is not None:
        body["model"] = model
    return Route("POST", "/v1/similarity", m.SimilarityResult, body)


def ner(text: str, labels: Optional[Sequence[str]]) -> Route:
    if labels is not None and (isinstance(labels, str) or not 1 <= len(labels) <= 18):
        raise InvalidRequestError("labels must be a list of 1-18 entity types")
    body = _drop_none({"text": _text(text, "text", 20000), "labels": list(labels) if labels is not None else None})
    return Route("POST", "/v1/ner", m.NerResult, body)


def classify_zero_shot(
    text: str, labels: Sequence[str], multi_label: Optional[bool], hypothesis_template: Optional[str]
) -> Route:
    if hypothesis_template is not None and (
        hypothesis_template.count("{}") != 1 or not 2 <= len(hypothesis_template) <= 200
    ):
        raise InvalidRequestError("hypothesis_template must contain {} exactly once (2-200 characters)")
    lab = _strings(labels, "labels", 10, 100)
    if len(set(lab)) != len(lab):
        raise InvalidRequestError("labels must be unique")
    body = _drop_none({"text": _text(text, "text", 2000), "labels": lab, "multi_label": multi_label,
                       "hypothesis_template": hypothesis_template})
    return Route("POST", "/v1/classify/zero-shot", m.ZeroShotResult, body)


# --- screening ------------------------------------------------------------


def check_url(url: Optional[str], domain: Optional[str]) -> Route:
    if (url is None) == (domain is None):
        raise InvalidRequestError("give url or domain, not both")
    body = {"url": _text(url, "url", 2048)} if url is not None else {"domain": _text(domain, "domain", 2048)}
    return Route("POST", "/v1/check/url", m.UrlCheck, body)


def check_urls(items: Sequence[str]) -> Route:
    return Route("POST", "/v1/check/url/batch", m.UrlCheckBatch, {"items": _strings(items, "items", 1000, 2048)})


_SANCTION_ADDR = re.compile(r"^[A-Za-z0-9:_.\-]{1,128}$")


def sanctions_batch(addresses: Sequence[str]) -> Route:
    addrs = _strings(addresses, "addresses", 1000, 128)
    if any(not _SANCTION_ADDR.match(a) for a in addrs):
        raise InvalidRequestError("addresses may only contain letters, digits and : _ . -")
    return Route("POST", "/v1/sanctions/batch", m.SanctionsBatch, {"addresses": addrs})


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
