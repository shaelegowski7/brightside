// Mirrors app/schemas.py's ScanResponse -- keep in sync by hand (no shared
// schema generation for a 3-user tool; see pwa/README for that call).
export interface ScanResponse {
  verdict: "PASS" | "PASS_WITH_FLAGS" | "REJECT";
  reasons: string[];
  flags: string[];
  asin: string | null;
  match_confidence: string | null;
  buy_price_pence: number;
  sell_price_pence: number | null;
  net_profit_pence: number | null;
  roi: number | null;
  posted_to_discord: boolean;
  keepa_url: string | null;
  amazon_url: string | null;
}

export interface ApiError {
  status: number;
  message: string;
}
