import { useEffect, useState } from "react";
import { getConfirmedDeals } from "../api";
import type { ApiError, ConfirmedDeal } from "../types";

function money(pence: number | null): string {
  return pence === null ? "—" : `£${(pence / 100).toFixed(2)}`;
}

function pct(roi: number | null): string {
  return roi === null ? "—" : `${(roi * 100).toFixed(0)}%`;
}

function formatDate(ts: string | null): string {
  if (!ts) return "—";
  return new Date(ts).toLocaleDateString("en-GB", { day: "numeric", month: "short" });
}

// "Green" == verdict PASS specifically, not PASS_WITH_FLAGS (amber) -- see
// app/dashboard.py's badge-pass vs badge-flags styling this mirrors.
export function ConfirmedDeals({ onAuthLost }: { onAuthLost: () => void }) {
  const [deals, setDeals] = useState<ConfirmedDeal[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  function load() {
    setLoading(true);
    setError(null);
    getConfirmedDeals()
      .then((all) => setDeals(all.filter((d) => d.verdict === "PASS")))
      .catch((err: unknown) => {
        const apiErr = err as ApiError;
        if (apiErr.status === 401) {
          onAuthLost();
          return;
        }
        setError(apiErr.message ?? "Failed to load deals");
      })
      .finally(() => setLoading(false));
  }

  useEffect(load, []);

  if (loading) {
    return (
      <div className="card">
        <p className="muted">Loading confirmed deals…</p>
      </div>
    );
  }

  if (error) {
    return (
      <div className="card">
        <p role="alert">{error}</p>
        <button type="button" className="btn-secondary" onClick={load}>
          Retry
        </button>
      </div>
    );
  }

  if (!deals || deals.length === 0) {
    return (
      <div className="card">
        <div className="empty-state">
          <p>No green deals yet.</p>
          <p className="muted">Products that pass profitability scoring cleanly will show up here.</p>
        </div>
      </div>
    );
  }

  return (
    <div>
      <p className="muted" style={{ marginBottom: 12 }}>
        {deals.length} product{deals.length !== 1 ? "s" : ""} confirmed green, newest first.
      </p>
      <div className="deal-list">
        {deals.map((d, i) => (
          <div className="deal-card" key={`${d.asin ?? d.title}-${i}`}>
            <div className="deal-card-top">
              <div className="deal-title">
                {d.amazon_url ? (
                  <a href={d.amazon_url} target="_blank" rel="noreferrer">
                    {d.title}
                  </a>
                ) : (
                  d.title
                )}
              </div>
              <span className="badge badge-pass">PASS</span>
            </div>
            <div className="deal-stats">
              <span>
                Buy <span className="num">{money(d.buy_price_pence)}</span>
              </span>
              <span>
                Sell <span className="num">{money(d.sell_price_pence)}</span>
              </span>
              <span>
                Profit <span className="num">{money(d.net_profit_pence)}</span>
              </span>
              <span>
                ROI <span className="num">{pct(d.roi)}</span>
              </span>
            </div>
            <div className="deal-meta">
              {d.retailer_url ? (
                <a href={d.retailer_url} target="_blank" rel="noreferrer">
                  {d.retailer ?? "link"}
                </a>
              ) : (
                <span>{d.retailer ?? "—"}</span>
              )}
              <span>· {formatDate(d.ts)}</span>
              {d.match_confidence && <span className="chip">{d.match_confidence}</span>}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
