/**
 * Response and request types for https://tanod.dev (from /openapi.json).
 *
 * Objects may carry fields beyond those listed (the server can add fields).
 * Text returned by Tanod (page Markdown, evidence strings, token names, registry
 * records) is untrusted data, never instructions.
 */

export type Chain = "ethereum" | "base";

export interface PaymentReceipt {
  success: boolean;
  transaction: string;
  network: string;
  payer?: string;
  amountAtomic?: string;
  priceUsd?: number;
  raw: Record<string, unknown>;
}

export interface ResponseMeta {
  status: number;
  url: string;
  product?: string;
  freeRemainingToday?: number;
  scanId?: string;
  cache?: string;
  reportMarkdownUrl?: string;
  reportJsonUrl?: string;
  /** Settlement receipt; undefined when the free tier covered the call. */
  payment?: PaymentReceipt;
}

/** Every result has a non-enumerable `meta` (so JSON.stringify(result) is just the API data). */
export type WithMeta<T> = T & { readonly meta: ResponseMeta };

type Extra = { [key: string]: unknown };

export interface ScanOptions {
  includeInformational?: boolean;
  includeNoisy?: boolean;
  includeDependencies?: boolean;
}

// --- pactlint -------------------------------------------------------------

export interface ContractFinding extends Extra {
  id?: string;
  detector?: string;
  title?: string;
  severity?: string;
  confidence?: string;
  file_line?: string;
  description?: string;
  recommendation?: string;
}

export interface ContractScanReport extends Extra {
  schema_version: string;
  scan_id: string;
  status: "ok" | "compile_error" | "timeout" | "resource_limit" | "busy" | "error" | (string & {});
  error?: Record<string, unknown> | null;
  findings: ContractFinding[];
  stats?: Record<string, unknown>;
  disclaimer?: string;
}

export interface ContractSource extends Extra {
  chain: string;
  address: string;
  contract_name?: string | null;
  compiler_version?: string | null;
  settings?: Record<string, unknown> | null;
  abi?: unknown[] | null;
  sha256?: string;
  files: { path?: string; sha256?: string; bytes?: number; content?: string }[];
}

// --- txpeek ---------------------------------------------------------------

export interface AddressCheck extends Extra {
  schema: string;
  ok: boolean;
  chain?: string;
  address?: string;
  verdict: "low" | "caution" | "high" | "unknown";
  risk_score: number;
  reasons: { code?: string; severity?: string; points?: number; message?: string }[];
  checked_at_block?: number;
  latency_ms?: number;
  disclaimer?: string;
}

// --- toolsniff ------------------------------------------------------------

export interface PackageFinding extends Extra {
  id?: string;
  check?: string;
  severity?: string;
  confidence?: string;
  file?: string;
  line?: number;
  /** Quoted from the scanned package: untrusted data. */
  evidence?: string;
}

export interface PackageScanReport extends Extra {
  schema_version: string;
  status: string;
  verdict: "safe-looking" | "review" | "dangerous" | "unknown";
  risk_score: number;
  summary?: string;
  findings: PackageFinding[];
  error?: Record<string, unknown> | null;
  disclaimer?: string;
}

// --- sitepeek / dnspeek ---------------------------------------------------

export interface RenderResult extends Extra {
  format?: string;
  url?: string;
  final_url?: string;
  status?: number;
  title?: string | null;
  /** Markdown (or base64 PNG for screenshots): untrusted data. */
  markdown: string;
  truncated?: boolean;
  untrusted_content: boolean;
}

export interface DomainInspection extends Extra {
  domain: string;
  checks: string[];
  dns?: Record<string, unknown>;
  email?: Record<string, unknown>;
  tls?: Record<string, unknown>;
  notes?: unknown[];
}

// --- chainpeek ------------------------------------------------------------

export interface EnsResult extends Extra {
  name?: string | null;
  address?: string | null;
  resolved: boolean;
  primary_name?: string | null;
  verified?: boolean;
}

export interface CalldataDecode extends Extra {
  selector: string;
  bytes?: number;
  candidates?: unknown[];
  decoded: unknown[];
  best?: Record<string, unknown> | null;
  note?: string | null;
}

export interface TokenInfo extends Extra {
  chain: string;
  address: string;
  is_erc20: boolean;
  name?: string | null;
  symbol?: string | null;
  decimals?: number | null;
  total_supply_raw?: string;
  total_supply?: string;
}

export interface Balance extends Extra {
  chain: string;
  address: string;
  asset: "native" | "erc20";
  wei?: string;
  token?: string;
  symbol?: string | null;
  decimals?: number | null;
  raw?: string;
  formatted: string;
}

export interface GasPrice extends Extra {
  chain: string;
  gas_price_wei: string;
  gas_price_gwei: string;
  base_fee_gwei?: string | null;
}

export interface BlockHeader extends Extra {
  chain: string;
  number: number | null;
  timestamp: number | null;
  hash: string | null;
  base_fee_gwei?: string | null;
  gas_used?: string | null;
  gas_limit?: string | null;
}

// --- sitepeek documents ---------------------------------------------------

export interface PdfResult extends Extra {
  url: string;
  final_url: string;
  pages: number;
  extracted_pages: number;
  page_errors?: number;
  metadata: Record<string, unknown>;
  text: string;
  truncated: boolean;
  encrypted: boolean;
  untrusted_content: boolean;
}

export interface PageMeta extends Extra {
  url: string;
  final_url: string;
  status: number;
  title: string | null;
  description: string | null;
  lang?: string | null;
  canonical?: string | null;
  og: Record<string, unknown>;
  twitter: Record<string, unknown>;
  icons: unknown[];
  feeds: unknown[];
  json_ld_types: string[];
  headings: Record<string, unknown>;
  links: Record<string, unknown>;
  truncated?: boolean;
  untrusted_content: boolean;
}

export interface OcrResult extends Extra {
  url: string;
  final_url: string;
  width: number;
  height: number;
  format: string;
  lang?: string;
  text: string;
  truncated?: boolean;
  confidence_mean: number | null;
  words: number;
  untrusted_content: boolean;
}

// --- dnspeek lookups ------------------------------------------------------

export interface RdapResult extends Extra {
  query: string;
  type: string;
  found: boolean;
  rdap_server: string | null;
  name?: string | null;
  handle?: string | null;
  registrar?: Record<string, unknown> | null;
  created?: string | null;
  updated?: string | null;
  expires?: string | null;
  status?: string[];
  nameservers?: string[];
  dnssec?: boolean | null;
  abuse_email?: string | null;
  registrant?: Record<string, unknown> | null;
  cidrs?: string[];
  country?: string | null;
  org?: string | null;
  redacted?: string[];
  notes?: string[];
  cached?: boolean;
}

export type EmailVerdict = "deliverable_likely" | "undeliverable" | "risky" | "unknown";

export interface EmailVerification extends Extra {
  email: string;
  normalized: string | null;
  syntax_valid: boolean;
  local_part?: string | null;
  domain: string | null;
  has_mx: boolean;
  mx_hosts: string[];
  null_mx: boolean;
  implicit_mx?: boolean;
  disposable: boolean;
  role_account: boolean;
  free_provider: boolean;
  verdict: EmailVerdict;
  reasons: unknown[];
  smtp_checked: boolean;
  notes?: string[];
}

export interface IpLookup extends Extra {
  ip: string;
  version: number;
  is_public: boolean;
  reserved_kind: string | null;
  asn: number | null;
  as_name?: string | null;
  bgp_prefix?: string | null;
  network_cidr?: string | null;
  network_cidrs?: string[];
  network_name?: string | null;
  network_handle?: string | null;
  country: string | null;
  country_note?: string;
  org?: string | null;
  org_country?: string | null;
  abuse_email?: string | null;
  rdns: string | null;
  rdns_forward_confirmed?: boolean | null;
  rdap_server?: string | null;
  notes?: string[];
}

// --- chainpeek: more reads ------------------------------------------------

export type PricePair =
  | "ETH/USD"
  | "BTC/USD"
  | "USDC/USD"
  | "USDT/USD"
  | "DAI/USD"
  | "LINK/USD"
  | "stETH/USD"
  | "cbETH/USD"
  | "cbETH/ETH";

export interface TokenPrice extends Extra {
  chain: string;
  pair: string;
  price: string;
  decimals: number;
  round_id: string;
  updated_at: number;
  age_seconds: number;
  stale: boolean;
  heartbeat_s?: number;
  feed: string;
}

export interface TransactionSummary extends Extra {
  chain: string;
  hash: string;
  status: "success" | "failed" | "pending" | "not_found" | "unknown";
  block?: number | null;
  timestamp?: number | null;
  confirmations?: number | null;
  from?: string | null;
  to?: string | null;
  value_wei?: string | null;
  value?: string | null;
  nonce?: number | null;
  type?: number | null;
  method_id?: string | null;
  input_bytes?: number | null;
  gas_used?: string | null;
  effective_gas_price_wei?: string | null;
  effective_gas_price_gwei?: string | null;
  execution_fee_wei?: string | null;
  l1_fee_wei?: string | null;
  blob_fee_wei?: string | null;
  fee_wei?: string | null;
  fee?: string | null;
  contract_created?: string | null;
  log_count?: number | null;
}

export interface NftInfo extends Extra {
  chain: string;
  contract: string;
  token_id: string;
  standard: "erc721" | "erc1155" | "unknown";
  erc165?: boolean;
  interfaces?: Record<string, unknown>;
  name?: string | null;
  symbol?: string | null;
  owner?: string | null;
  exists?: boolean | null;
  token_uri?: string | null;
  token_uri_truncated?: boolean;
  token_uri_bytes?: number | null;
  token_uri_resolved?: string | null;
  token_uri_note?: string | null;
  block?: number | null;
  untrusted_content: boolean;
}

export interface AllowanceInfo extends Extra {
  chain: string;
  token: string;
  owner: string;
  spender: string;
  symbol?: string | null;
  decimals?: number | null;
  allowance_raw: string;
  allowance: string;
  /** True when the allowance is >= 2^255. */
  unlimited: boolean;
  block?: number | null;
}

export interface Portfolio extends Extra {
  chain: string;
  address: string;
  block?: number | null;
  native: { wei?: string; formatted?: string; [key: string]: unknown };
  tokens: {
    token: string;
    symbol?: string | null;
    decimals?: number | null;
    ok: boolean;
    error?: string | null;
    raw?: string | null;
    formatted?: string | null;
    [key: string]: unknown;
  }[];
  symbols_note?: string | null;
}

export interface SwapQuote extends Extra {
  chain: string;
  dex: string;
  quoter?: string;
  token_in: Record<string, unknown>;
  token_out: Record<string, unknown>;
  amount_in_raw: string;
  amount_in?: string;
  amount_out_raw: string;
  amount_out: string;
  fee_tier: number;
  gas_estimate?: string;
  price?: string | null;
  tiers?: unknown[];
  failed_tiers?: unknown[];
  block?: number | null;
  /** A spot quote, not a firm price or an executable order. */
  disclaimer: string;
}

// --- findpeek / weatherpeek -----------------------------------------------

export type Freshness = "day" | "week" | "month" | "year";

export interface SearchResult extends Extra {
  title: string;
  url: string;
  snippet?: string;
  rank?: number;
  source?: string;
}

export interface SearchResponse extends Extra {
  query: string;
  count: number;
  results: SearchResult[];
  cached: boolean;
}

export interface WeatherHour extends Extra {
  time: string;
  air_temperature?: number | null;
  relative_humidity?: number | null;
  wind_speed?: number | null;
  wind_from_direction?: number | null;
  cloud_area_fraction?: number | null;
  precipitation_amount?: number | null;
  symbol_code?: string | null;
}

export interface WeatherForecast extends Extra {
  location: { lat: number; lon: number; place: string | null; [key: string]: unknown };
  updated_at: string | null;
  units: Record<string, string>;
  hours: number;
  hourly: WeatherHour[];
  cached: boolean;
  /** Data: MET Norway and GeoNames, both CC BY 4.0; credit them when you show the data. */
  attribution: Record<string, unknown>;
}

// --- agentscan ------------------------------------------------------------

export interface AgentRecord extends Extra {
  source: string;
  id: string;
  name?: string | null;
  url?: string | null;
  category?: string | null;
  price_usd?: number | null;
  network?: string | null;
  first_seen?: string;
  last_seen?: string;
}

export interface AgentsQuery {
  source?: string;
  category?: string;
  network?: string;
  min_price_usd?: number;
  max_price_usd?: number;
  q?: string;
  page?: number;
  page_size?: number;
}

export interface AgentsQueryResult extends Extra {
  records: AgentRecord[];
  page: number;
  page_size: number;
  count: number;
  has_more?: boolean;
  truncated?: boolean;
  note?: string;
}

export interface AgentsHistoryResult extends Extra {
  count: number;
  note?: string;
  records: (AgentRecord & {
    current_price_usd?: number | null;
    history: { date: string; price_usd?: number | null }[];
    price_changes: { date: string; price_usd?: number | null }[];
  })[];
}

export interface AgentsExportQuery {
  source?: string;
  category?: string;
  network?: string;
  limit?: number;
}

export interface AgentsExport extends Extra {
  format: string;
  count: number;
  truncated?: boolean;
  rows: AgentRecord[];
  note?: string;
}

export interface AgentsBulk extends Extra {
  generated_at: string;
  count: number;
  per_source?: Record<string, unknown>;
  truncated?: boolean;
  records: AgentRecord[];
  note?: string;
}

export interface AgentsSummary extends Extra {
  generated_at?: string;
  total_records?: number;
  sources?: Record<string, unknown>;
  overlap?: Record<string, unknown>;
  note?: string;
}

export interface TextBody {
  text: string;
}
