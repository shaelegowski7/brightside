import { useEffect, useRef, useState } from "react";
import { connectCrawlWebSocket, triggerCrawl } from "../api";
import type { ApiError, CrawlStatus } from "../types";

interface CrawlPanelProps {
  onAuthLost: () => void;
  onFinished: () => void;
}

export function CrawlPanel({ onAuthLost, onFinished }: CrawlPanelProps) {
  const [status, setStatus] = useState<CrawlStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [starting, setStarting] = useState(false);
  const wasRunning = useRef(false);

  function handleApiError(err: unknown): void {
    const apiErr = err as ApiError;
    if (apiErr.status === 401) {
      onAuthLost();
      return;
    }
    setError(apiErr.message ?? "Something went wrong");
  }

  // Connected for as long as this panel (i.e. the Green Deals tab) is
  // mounted -- not just while a crawl is running -- so a run started from
  // another tab/device shows up here immediately instead of waiting for
  // the next mount.
  useEffect(() => {
    const disconnect = connectCrawlWebSocket(setStatus, handleApiError);
    return disconnect;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (wasRunning.current && status && !status.running) {
      onFinished();
    }
    wasRunning.current = status?.running ?? false;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [status?.running]);

  async function handleRunClick() {
    setStarting(true);
    setError(null);
    try {
      const result = await triggerCrawl();
      setStatus(result);
    } catch (err) {
      handleApiError(err);
    } finally {
      setStarting(false);
    }
  }

  const running = status?.running ?? false;
  const doneCount = status?.sources.filter((s) => s.status === "done" || s.status === "error").length ?? 0;
  const totalActive = status?.sources.filter((s) => s.status !== "skipped").length ?? 0;
  const totalNewDeals = status?.sources.reduce((sum, s) => sum + (s.new_deals ?? 0), 0) ?? 0;
  const justFinished = status && !status.running && status.finished_at;

  return (
    <div className="card crawl-panel">
      <div className="crawl-header">
        <div>
          <p className="crawl-title">Crawl retailers</p>
          <p className="crawl-subtitle">
            {running
              ? `Running… ${doneCount}/${totalActive} sources done`
              : "Pull fresh listings from every retailer right now"}
          </p>
        </div>
        <button type="button" className="btn-primary crawl-run-btn" onClick={handleRunClick} disabled={running || starting}>
          {running || starting ? (
            <>
              <span className="crawl-icon running" aria-hidden="true" />
              Crawling…
            </>
          ) : (
            "Run crawl now"
          )}
        </button>
      </div>

      {error && <p role="alert" style={{ marginTop: 12 }}>{error}</p>}

      {status && status.sources.length > 0 && (
        <ul className="crawl-source-list">
          {status.sources.map((s) => (
            <li key={s.key} className={`crawl-source-row is-${s.status}`}>
              <span className={`crawl-icon ${s.status}`} aria-hidden="true">
                {s.status === "done" && "✓"}
                {s.status === "error" && "✕"}
              </span>
              <span className="crawl-source-label">
                {s.label}
                {s.status === "error" && s.error && <span className="crawl-error-text">{s.error}</span>}
              </span>
              <span className={`crawl-source-result ${s.new_deals ? "has-new" : ""}`}>
                {s.status === "pending" && "waiting…"}
                {s.status === "running" && "crawling…"}
                {s.status === "skipped" && "disabled"}
                {s.status === "done" && `${s.new_deals} new`}
                {s.status === "error" && "failed"}
              </span>
            </li>
          ))}
        </ul>
      )}

      {justFinished && (
        <p className="crawl-summary">
          Done — {totalNewDeals} new deal{totalNewDeals !== 1 ? "s" : ""} found across {totalActive} source
          {totalActive !== 1 ? "s" : ""}.
        </p>
      )}
    </div>
  );
}
