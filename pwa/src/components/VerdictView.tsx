import type { ScanResponse } from "../types";

function money(pence: number | null): string {
  return pence === null ? "—" : `£${(pence / 100).toFixed(2)}`;
}

function pct(roi: number | null): string {
  return roi === null ? "—" : `${(roi * 100).toFixed(0)}%`;
}

const VERDICT_LABEL: Record<ScanResponse["verdict"], string> = {
  PASS: "✅ PASS",
  PASS_WITH_FLAGS: "⚠️ PASS (with flags)",
  REJECT: "❌ REJECT",
};

interface VerdictViewProps {
  result: ScanResponse;
  onScanAnother: () => void;
}

export function VerdictView({ result, onScanAnother }: VerdictViewProps) {
  return (
    <div>
      <h2>{VERDICT_LABEL[result.verdict]}</h2>

      {result.verdict !== "REJECT" && (
        <dl>
          <dt>Buy price</dt>
          <dd>{money(result.buy_price_pence)}</dd>
          <dt>Est. sell price</dt>
          <dd>{money(result.sell_price_pence)}</dd>
          <dt>Net profit</dt>
          <dd>{money(result.net_profit_pence)}</dd>
          <dt>ROI</dt>
          <dd>{pct(result.roi)}</dd>
        </dl>
      )}

      {result.reasons.length > 0 && (
        <ul>
          {result.reasons.map((r) => (
            <li key={r}>{r}</li>
          ))}
        </ul>
      )}
      {result.flags.length > 0 && (
        <p>Flags: {result.flags.join(", ")}</p>
      )}

      <p>
        {result.posted_to_discord
          ? "Posted to Discord."
          : "Not posted to Discord (rejected, or a recent ping already covers this item)."}
      </p>

      {result.asin && (
        <p>
          {result.keepa_url && <a href={result.keepa_url} target="_blank" rel="noreferrer">Keepa chart</a>}
          {" · "}
          {result.amazon_url && <a href={result.amazon_url} target="_blank" rel="noreferrer">Amazon listing</a>}
        </p>
      )}

      <button type="button" onClick={onScanAnother}>Scan another item</button>
    </div>
  );
}
