"""Sync (``Tanod``) and async (``AsyncTanod``) clients for https://tanod.dev.

Flow for every call:

1. Send the request unpaid. If the free daily tier covers it, the answer is a 200.
2. On ``402 Payment Required``: if the 402 says the input would be refused, raise
   ``InvalidRequestError`` (nothing is signed). Without a wallet, raise
   ``PaymentRequiredError`` with the quoted price. Above ``max_price_usd``, raise
   ``PriceLimitExceededError``. Otherwise sign an x402 payment (USDC on Base) with
   the official ``x402`` package and retry once with ``PAYMENT-SIGNATURE``.
3. 429 and 503 are retried (honouring ``Retry-After``); they are never charged.
4. The settlement receipt (``PAYMENT-RESPONSE``) is returned in ``result.meta.payment``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from decimal import Decimal
from typing import Any, Literal, Optional, Sequence, TypeVar, overload

import httpx

from . import _models as m
from . import _routes as r
from ._errors import (
    InvalidRequestError,
    NotFoundError,
    PaymentError,
    PaymentRequiredError,
    PriceLimitExceededError,
    RateLimitError,
    ServiceUnavailableError,
    TanodError,
)
from ._payment import (
    atomic_to_usd,
    build_payment_client,
    decode_receipt,
    first_requirement,
    parse_payment_required,
)
from ._version import __version__

logger = logging.getLogger("tanod")

DEFAULT_BASE_URL = "https://tanod.dev"
DEFAULT_TIMEOUT = 120.0  # scans may take up to ~90 s server-side
DEFAULT_MAX_PRICE_USD = 1.0
BASE_MAINNET = "eip155:8453"
USDC_BASE = "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913"  # USDC on Base mainnet (6 decimals)
_RETRYABLE = (429, 503)

T = TypeVar("T", bound=m.TanodModel)


# --------------------------------------------------------------------------
# Shared, IO-free helpers
# --------------------------------------------------------------------------


def _json_or_none(resp: httpx.Response) -> Any:
    try:
        return resp.json()
    except (json.JSONDecodeError, ValueError, UnicodeDecodeError):
        return None


def _error_text(body: Any, resp: httpx.Response) -> str:
    if isinstance(body, dict):
        err = body.get("error", body.get("detail"))
        if isinstance(err, dict):
            code = err.get("code")
            msg = err.get("message") or err.get("detail") or json.dumps(err)[:300]
            return f"{code}: {msg}" if code else str(msg)
        if err:
            return str(err)[:500]
    return (resp.text or resp.reason_phrase or "")[:500]


def _retry_after(resp: httpx.Response) -> Optional[float]:
    value = resp.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None


def _int_header(resp: httpx.Response, name: str) -> Optional[int]:
    value = resp.headers.get(name)
    try:
        return int(value) if value is not None else None
    except ValueError:
        return None


class _Quote:
    """A parsed 402 and the decision about it."""

    def __init__(self, resp: httpx.Response, body: Any) -> None:
        self.payment_required = parse_payment_required(resp.headers.get, body)
        req = first_requirement(self.payment_required) if self.payment_required else None
        self.amount: Optional[str] = getattr(req, "amount", None)
        self.price_usd: Optional[Decimal] = atomic_to_usd(self.amount)
        self.asset = getattr(req, "asset", None)
        self.network = getattr(req, "network", None)
        self.pay_to = getattr(req, "pay_to", None)
        err = getattr(self.payment_required, "error", None)
        if err is None and isinstance(body, dict):
            err = body.get("error") if isinstance(body.get("error"), str) else None
        self.error: Optional[str] = err
        self.body = body

    @property
    def input_refused(self) -> bool:
        return bool(self.error and "would be refused" in self.error)

    def error_kwargs(self) -> dict[str, Any]:
        return dict(
            price_usd=self.price_usd,
            amount_atomic=self.amount,
            asset=self.asset,
            network=self.network,
            pay_to=self.pay_to,
            reason=self.error,
            payment_required=self.payment_required,
            body=self.body,
        )


def _check_quote(quote: _Quote, path: str, has_wallet: bool, max_price: Optional[float]) -> None:
    """Raise unless the client should sign a payment for this quote."""
    if quote.input_refused:
        raise InvalidRequestError(quote.error or "input refused", status_code=402, body=quote.body)
    if quote.payment_required is None or quote.amount is None:
        raise TanodError(f"402 from {path} without usable x402 requirements", status_code=402, body=quote.body)
    # Fail closed: this SDK only ever pays USDC on Base mainnet, and only when the price is known,
    # so a malformed or unexpected quote can never slip past the max_price_usd cap.
    if quote.network != BASE_MAINNET or str(quote.asset or "").lower() != USDC_BASE:
        raise TanodError(
            f"{path} quotes an unsupported payment (network={quote.network}, asset={quote.asset}); "
            "this SDK only pays USDC on Base mainnet. Nothing was signed",
            status_code=402, body=quote.body,
        )
    if quote.price_usd is None:
        raise TanodError(f"{path} quotes an unparseable amount {quote.amount!r}; nothing was signed",
                         status_code=402, body=quote.body)
    if not has_wallet:
        raise PaymentRequiredError(
            f"{path} costs USD {quote.price_usd} (free daily tier used up or not available). "
            "Configure a wallet (TANOD_PRIVATE_KEY or signer=...) to pay with USDC on Base.",
            **quote.error_kwargs(),
        )
    if max_price is not None and quote.price_usd is not None and quote.price_usd > Decimal(str(max_price)):
        raise PriceLimitExceededError(
            f"{path} quotes USD {quote.price_usd}, above max_price_usd={max_price}; nothing was signed",
            **quote.error_kwargs(),
        )


def _raise_for_status(resp: httpx.Response, body: Any, path: str) -> None:
    status = resp.status_code
    if status < 400:
        return
    text = _error_text(body, resp)
    if status in (413, 415, 422):
        raise InvalidRequestError(f"{path}: {text}", status_code=status, body=body)
    if status == 404:
        raise NotFoundError(f"{path}: {text}", status_code=status, body=body)
    if status == 429:
        raise RateLimitError(f"{path}: rate limited: {text}", retry_after=_retry_after(resp), status_code=status, body=body)
    if status in (502, 503, 504):
        raise ServiceUnavailableError(
            f"{path}: temporarily unavailable ({status}, not charged): {text}",
            retry_after=_retry_after(resp),
            status_code=status,
            body=body,
        )
    raise TanodError(f"{path}: HTTP {status}: {text}", status_code=status, body=body)


def _parse(route: r.Route, resp: httpx.Response, body: Any, quote: Optional[_Quote]) -> Any:
    receipt = decode_receipt(resp.headers.get, quote.price_usd if quote else None, quote.amount if quote else None)
    meta = m.ResponseMeta(
        status_code=resp.status_code,
        url=str(resp.request.url) if resp.request is not None else "",
        product=resp.headers.get("X-Tanod-Product"),
        free_remaining_today=_int_header(resp, "X-Free-Remaining-Today"),
        scan_id=resp.headers.get("X-Scan-Id"),
        cache=resp.headers.get("X-Cache"),
        report_markdown_url=resp.headers.get("X-Report-Markdown"),
        report_json_url=resp.headers.get("X-Report-Json"),
        payment=receipt,
    )
    if route.kind == "csv":
        model: m.TanodModel = m.CsvExport(csv=resp.text)
    elif route.kind == "markdown":
        model = m.TextReport(markdown=resp.text)
    else:
        if not isinstance(body, dict):
            raise TanodError(f"{route.path}: expected a JSON object", status_code=resp.status_code, body=resp.text[:500])
        model = route.model.model_validate(body)
    model._meta = meta
    return model


class _Base:
    def __init__(
        self,
        *,
        base_url: str,
        max_price_usd: Optional[float],
        max_retries: int,
        max_retry_wait: float,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.max_price_usd = max_price_usd
        self.max_retries = max(0, int(max_retries))
        self.max_retry_wait = max_retry_wait
        self._payer: Any = None

    @property
    def has_wallet(self) -> bool:
        """True when a payment signer is configured."""
        return self._payer is not None

    def _headers(self) -> dict[str, str]:
        return {"User-Agent": f"tanod-python/{__version__}", "Accept": "application/json, text/plain, */*"}

    def _wait_for(self, resp: httpx.Response, attempt: int) -> Optional[float]:
        """Seconds to wait before retrying, or None to stop retrying."""
        if resp.status_code not in _RETRYABLE or attempt >= self.max_retries:
            return None
        wait = _retry_after(resp)
        if wait is None:
            wait = min(2.0**attempt, 8.0)
        if wait > self.max_retry_wait:
            return None
        return wait

    def __repr__(self) -> str:  # never show key material
        return f"{type(self).__name__}(base_url={self.base_url!r}, wallet={'yes' if self.has_wallet else 'no'})"


# --------------------------------------------------------------------------
# Sync client
# --------------------------------------------------------------------------


class Tanod(_Base):
    """Synchronous Tanod client.

    Args:
        private_key: hex private key of the paying wallet (USDC on Base). Defaults to
            the ``TANOD_PRIVATE_KEY`` environment variable. Never logged or stored as text.
        signer: an ``eth_account`` account or an x402 ``ClientEvmSigner`` (instead of a key).
        payment_client: a fully configured ``x402.x402ClientSync`` (advanced; overrides both).
        max_price_usd: refuse to sign any single payment above this (default USD 1.00;
            ``agents_bulk`` costs USD 2.00). ``None`` disables the cap.
        use_env: read ``TANOD_PRIVATE_KEY`` when no key/signer is given (default True).
        max_retries: retries on 429/503 (never charged). Default 2.
        max_retry_wait: give up instead of sleeping longer than this many seconds.
        timeout: HTTP timeout in seconds (scans can take ~90 s).
        http_client: your own ``httpx.Client`` (for proxies, testing).

    Without a wallet the client still works for free-tier calls and the free
    ``agents_summary``; past the free tier it raises ``PaymentRequiredError`` with the price.
    """

    def __init__(
        self,
        *,
        private_key: Optional[str] = None,
        signer: Any = None,
        payment_client: Any = None,
        max_price_usd: Optional[float] = DEFAULT_MAX_PRICE_USD,
        use_env: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 2,
        max_retry_wait: float = 30.0,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.Client] = None,
    ) -> None:
        super().__init__(
            base_url=base_url, max_price_usd=max_price_usd, max_retries=max_retries, max_retry_wait=max_retry_wait
        )
        self._payer = build_payment_client(
            private_key=private_key,
            signer=signer,
            payment_client=payment_client,
            max_price_usd=max_price_usd,
            use_env=use_env,
            is_async=False,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)

    def close(self) -> None:
        if self._owns_http:
            self._http.close()

    def __enter__(self) -> "Tanod":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # -- transport ---------------------------------------------------------

    def _request(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Response:
        headers = self._headers()
        if extra:
            headers.update(extra)
        return self._http.request(route.method, self.base_url + route.path, json=route.json, headers=headers)

    def _call(self, route: r.Route) -> Any:
        attempt = 0
        while True:
            resp = self._request(route)
            quote: Optional[_Quote] = None
            if resp.status_code == 402:
                quote = _Quote(resp, _json_or_none(resp))
                _check_quote(quote, route.path, self.has_wallet, self.max_price_usd)
                try:
                    payload = self._payer.create_payment_payload(quote.payment_required)
                    from x402.http.x402_http_client_base import x402HTTPClientBase

                    pay_headers = x402HTTPClientBase().encode_payment_signature_header(payload)
                except Exception as exc:
                    raise PaymentError(f"could not build the x402 payment: {type(exc).__name__}: {exc}") from exc
                logger.info("tanod: paying USD %s for %s %s", quote.price_usd, route.method, route.path)
                resp = self._request(route, pay_headers)
                if resp.status_code == 402:
                    again = _Quote(resp, _json_or_none(resp))
                    raise PaymentRequiredError(
                        f"{route.path}: payment refused: {again.error or 'no reason given'}",
                        **again.error_kwargs(),
                    )
            body = None if route.kind != "json" and resp.status_code < 400 else _json_or_none(resp)
            wait = self._wait_for(resp, attempt)
            if wait is not None:
                logger.info("tanod: %s %s -> %s, retrying in %.1fs", route.method, route.path, resp.status_code, wait)
                time.sleep(wait)
                attempt += 1
                continue
            _raise_for_status(resp, body, route.path)
            return _parse(route, resp, body, quote)

    # -- pactlint ----------------------------------------------------------

    def scan_contract_source(
        self,
        source: Optional[str] = None,
        *,
        standard_json: Optional[dict[str, Any]] = None,
        filename: Optional[str] = None,
        compiler_version: Optional[str] = None,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        """pactlint: static analysis of Solidity source (one file, or solc standard JSON).

        USD 0.25 up to 3,000 nSLOC, USD 0.75 up to 15,000. Free tier: 3 scans/IP/day.
        """
        return self._call(
            r.scan_contract_source(
                source, standard_json, filename, compiler_version, include_informational, include_noisy, include_dependencies
            )
        )

    def scan_contract_address(
        self,
        address: str,
        chain: m.Chain = "ethereum",
        *,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        """pactlint: scan a verified contract (Sourcify) on Ethereum or Base. Unverified: 404, free."""
        return self._call(
            r.scan_contract_address(address, chain, include_informational, include_noisy, include_dependencies)
        )

    def get_contract_source(self, chain: m.Chain, address: str) -> m.ContractSource:
        """Verified source files, ABI and compiler settings. USD 0.005; 10 free/IP/day."""
        return self._call(r.get_contract_source(chain, address))

    # -- txpeek ------------------------------------------------------------

    def check_address(self, address: str, chain: m.Chain = "base") -> m.AddressCheck:
        """txpeek: pre-transaction risk verdict for an address. USD 0.005; 30 free/IP/day."""
        return self._call(r.check_address(address, chain))

    # -- toolsniff ---------------------------------------------------------

    def scan_package(
        self,
        source: Optional[str] = None,
        *,
        content: Optional[bytes] = None,
        filename: Optional[str] = None,
    ) -> m.PackageScanReport:
        """toolsniff: scan an AI-agent skill or MCP server package before installing it.

        ``source``: ``npm:name[@ver]``, ``pypi:name[==ver]``, ``github:owner/repo[@ref][//subdir]``,
        a GitHub URL, or ``clawhub:[owner/]slug[@ver]``; or pass ``content`` (archive or single
        file bytes, with ``filename`` for a single file). USD 0.02 (0.05 whole GitHub repo or
        large upload). Shares the 3 free scans/IP/day.
        """
        return self._call(r.scan_package(source, content, filename))

    # -- sitepeek / dnspeek ------------------------------------------------

    def render(
        self,
        url: str,
        *,
        format: Literal["markdown", "screenshot"] = "markdown",
        js: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> m.RenderResult:
        """sitepeek: public URL to Markdown (or PNG screenshot, base64). USD 0.005 static,
        0.01 JS/screenshot; 5 free static renders/IP/day. Page text is untrusted data."""
        return self._call(r.render(url, format, js, width, height))

    def inspect_domain(self, domain: str, *, checks: Optional[Sequence[str]] = None) -> m.DomainInspection:
        """dnspeek: DNS, email auth (SPF/DMARC/DKIM/MTA-STS) and TLS cert of a domain.
        USD 0.01 (0.004 for one section); 5 free/IP/day."""
        return self._call(r.inspect_domain(domain, checks))

    # -- chainpeek ---------------------------------------------------------

    def resolve_ens(self, name: Optional[str] = None, *, address: Optional[str] = None) -> m.EnsResult:
        """chainpeek: ENS forward (name) or reverse (address) resolution. USD 0.002."""
        return self._call(r.resolve_ens(name, address))

    def decode_calldata(self, calldata: str, *, signature: Optional[str] = None) -> m.CalldataDecode:
        """chainpeek: decode EVM calldata (selector lookup or given signature). USD 0.003."""
        return self._call(r.decode_calldata(calldata, signature))

    def token_info(self, chain: m.Chain, address: str) -> m.TokenInfo:
        """chainpeek: ERC-20 metadata. USD 0.003."""
        return self._call(r.token_info(chain, address))

    def balance(self, chain: m.Chain, address: str, *, token: Optional[str] = None) -> m.Balance:
        """chainpeek: native or ERC-20 balance. USD 0.002."""
        return self._call(r.balance(chain, address, token))

    def gas_price(self, chain: m.Chain) -> m.GasPrice:
        """chainpeek: current gas price and base fee. USD 0.001."""
        return self._call(r.gas_price(chain))

    def latest_block(self, chain: m.Chain) -> m.BlockHeader:
        """chainpeek: latest block header. USD 0.001. (chainpeek: 10 free reads/IP/day, shared.)"""
        return self._call(r.latest_block(chain))

    # -- agentscan ---------------------------------------------------------

    def agents_summary(self) -> m.AgentsSummary:
        """agentscan: free aggregate summary of the x402/MCP index (no payment, no quota)."""
        return self._call(r.agents_summary())

    def agents_query(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        min_price_usd: Optional[float] = None,
        max_price_usd: Optional[float] = None,
        q: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> m.AgentsQueryResult:
        """agentscan: filtered, paginated index records. USD 0.02; 10 free/IP/day."""
        return self._call(
            r.agents_query(source, category, network, min_price_usd, max_price_usd, q, page, page_size)
        )

    def agents_history(
        self, *, source: Optional[str] = None, id: Optional[str] = None, url: Optional[str] = None
    ) -> m.AgentsHistoryResult:
        """agentscan: presence and price history of one record. USD 0.05; 5 free/IP/day."""
        return self._call(r.agents_history(source, id, url))

    @overload
    def agents_export(
        self,
        *,
        source: Optional[str] = ...,
        category: Optional[str] = ...,
        network: Optional[str] = ...,
        format: Literal["json"] = ...,
        limit: Optional[int] = ...,
    ) -> m.AgentsExport: ...

    @overload
    def agents_export(
        self,
        *,
        source: Optional[str] = ...,
        category: Optional[str] = ...,
        network: Optional[str] = ...,
        format: Literal["csv"],
        limit: Optional[int] = ...,
    ) -> m.CsvExport: ...

    def agents_export(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        format: Literal["json", "csv"] = "json",
        limit: Optional[int] = None,
    ) -> Any:
        """agentscan: filtered slice of the index (<= 5,000 rows), JSON or CSV. USD 0.25; no free tier."""
        return self._call(r.agents_export(source, category, network, format, limit))

    def agents_bulk(self, *, source: Optional[str] = None) -> m.AgentsBulk:
        """agentscan: full current snapshot. USD 2.00 (raise max_price_usd); no free tier."""
        return self._call(r.agents_bulk(source))

    # -- shared ------------------------------------------------------------

    def get_report(self, scan_id: str, *, format: Literal["json", "markdown"] = "json") -> Any:
        """A stored pactlint/toolsniff report (kept 30 days; free to re-read)."""
        return self._call(r.get_report(scan_id, format))

    def health(self) -> m.Health:
        """Liveness and configuration summary (free)."""
        return self._call(r.health())


# --------------------------------------------------------------------------
# Async client
# --------------------------------------------------------------------------


class AsyncTanod(_Base):
    """Asynchronous Tanod client; same arguments and methods as :class:`Tanod`.

    ``payment_client`` must be an async ``x402.x402Client`` when given.
    """

    def __init__(
        self,
        *,
        private_key: Optional[str] = None,
        signer: Any = None,
        payment_client: Any = None,
        max_price_usd: Optional[float] = DEFAULT_MAX_PRICE_USD,
        use_env: bool = True,
        base_url: str = DEFAULT_BASE_URL,
        max_retries: int = 2,
        max_retry_wait: float = 30.0,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        super().__init__(
            base_url=base_url, max_price_usd=max_price_usd, max_retries=max_retries, max_retry_wait=max_retry_wait
        )
        self._payer = build_payment_client(
            private_key=private_key,
            signer=signer,
            payment_client=payment_client,
            max_price_usd=max_price_usd,
            use_env=use_env,
            is_async=True,
        )
        self._owns_http = http_client is None
        self._http = http_client or httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        if self._owns_http:
            await self._http.aclose()

    async def __aenter__(self) -> "AsyncTanod":
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def _request(self, route: r.Route, extra: Optional[dict[str, str]] = None) -> httpx.Response:
        headers = self._headers()
        if extra:
            headers.update(extra)
        return await self._http.request(route.method, self.base_url + route.path, json=route.json, headers=headers)

    async def _call(self, route: r.Route) -> Any:
        attempt = 0
        while True:
            resp = await self._request(route)
            quote: Optional[_Quote] = None
            if resp.status_code == 402:
                quote = _Quote(resp, _json_or_none(resp))
                _check_quote(quote, route.path, self.has_wallet, self.max_price_usd)
                try:
                    payload = self._payer.create_payment_payload(quote.payment_required)
                    if asyncio.iscoroutine(payload):
                        payload = await payload
                    from x402.http.x402_http_client_base import x402HTTPClientBase

                    pay_headers = x402HTTPClientBase().encode_payment_signature_header(payload)
                except Exception as exc:
                    raise PaymentError(f"could not build the x402 payment: {type(exc).__name__}: {exc}") from exc
                logger.info("tanod: paying USD %s for %s %s", quote.price_usd, route.method, route.path)
                resp = await self._request(route, pay_headers)
                if resp.status_code == 402:
                    again = _Quote(resp, _json_or_none(resp))
                    raise PaymentRequiredError(
                        f"{route.path}: payment refused: {again.error or 'no reason given'}",
                        **again.error_kwargs(),
                    )
            body = None if route.kind != "json" and resp.status_code < 400 else _json_or_none(resp)
            wait = self._wait_for(resp, attempt)
            if wait is not None:
                logger.info("tanod: %s %s -> %s, retrying in %.1fs", route.method, route.path, resp.status_code, wait)
                await asyncio.sleep(wait)
                attempt += 1
                continue
            _raise_for_status(resp, body, route.path)
            return _parse(route, resp, body, quote)

    async def scan_contract_source(
        self,
        source: Optional[str] = None,
        *,
        standard_json: Optional[dict[str, Any]] = None,
        filename: Optional[str] = None,
        compiler_version: Optional[str] = None,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        return await self._call(
            r.scan_contract_source(
                source, standard_json, filename, compiler_version, include_informational, include_noisy, include_dependencies
            )
        )

    async def scan_contract_address(
        self,
        address: str,
        chain: m.Chain = "ethereum",
        *,
        include_informational: Optional[bool] = None,
        include_noisy: Optional[bool] = None,
        include_dependencies: Optional[bool] = None,
    ) -> m.ContractScanReport:
        return await self._call(
            r.scan_contract_address(address, chain, include_informational, include_noisy, include_dependencies)
        )

    async def get_contract_source(self, chain: m.Chain, address: str) -> m.ContractSource:
        return await self._call(r.get_contract_source(chain, address))

    async def check_address(self, address: str, chain: m.Chain = "base") -> m.AddressCheck:
        return await self._call(r.check_address(address, chain))

    async def scan_package(
        self, source: Optional[str] = None, *, content: Optional[bytes] = None, filename: Optional[str] = None
    ) -> m.PackageScanReport:
        return await self._call(r.scan_package(source, content, filename))

    async def render(
        self,
        url: str,
        *,
        format: Literal["markdown", "screenshot"] = "markdown",
        js: bool = False,
        width: Optional[int] = None,
        height: Optional[int] = None,
    ) -> m.RenderResult:
        return await self._call(r.render(url, format, js, width, height))

    async def inspect_domain(self, domain: str, *, checks: Optional[Sequence[str]] = None) -> m.DomainInspection:
        return await self._call(r.inspect_domain(domain, checks))

    async def resolve_ens(self, name: Optional[str] = None, *, address: Optional[str] = None) -> m.EnsResult:
        return await self._call(r.resolve_ens(name, address))

    async def decode_calldata(self, calldata: str, *, signature: Optional[str] = None) -> m.CalldataDecode:
        return await self._call(r.decode_calldata(calldata, signature))

    async def token_info(self, chain: m.Chain, address: str) -> m.TokenInfo:
        return await self._call(r.token_info(chain, address))

    async def balance(self, chain: m.Chain, address: str, *, token: Optional[str] = None) -> m.Balance:
        return await self._call(r.balance(chain, address, token))

    async def gas_price(self, chain: m.Chain) -> m.GasPrice:
        return await self._call(r.gas_price(chain))

    async def latest_block(self, chain: m.Chain) -> m.BlockHeader:
        return await self._call(r.latest_block(chain))

    async def agents_summary(self) -> m.AgentsSummary:
        return await self._call(r.agents_summary())

    async def agents_query(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        min_price_usd: Optional[float] = None,
        max_price_usd: Optional[float] = None,
        q: Optional[str] = None,
        page: Optional[int] = None,
        page_size: Optional[int] = None,
    ) -> m.AgentsQueryResult:
        return await self._call(
            r.agents_query(source, category, network, min_price_usd, max_price_usd, q, page, page_size)
        )

    async def agents_history(
        self, *, source: Optional[str] = None, id: Optional[str] = None, url: Optional[str] = None
    ) -> m.AgentsHistoryResult:
        return await self._call(r.agents_history(source, id, url))

    async def agents_export(
        self,
        *,
        source: Optional[str] = None,
        category: Optional[str] = None,
        network: Optional[str] = None,
        format: Literal["json", "csv"] = "json",
        limit: Optional[int] = None,
    ) -> Any:
        return await self._call(r.agents_export(source, category, network, format, limit))

    async def agents_bulk(self, *, source: Optional[str] = None) -> m.AgentsBulk:
        return await self._call(r.agents_bulk(source))

    async def get_report(self, scan_id: str, *, format: Literal["json", "markdown"] = "json") -> Any:
        return await self._call(r.get_report(scan_id, format))

    async def health(self) -> m.Health:
        return await self._call(r.health())
