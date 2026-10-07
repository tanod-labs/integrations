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
