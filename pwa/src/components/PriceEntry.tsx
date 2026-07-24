import { useState, type FormEvent } from "react";

interface PriceEntryProps {
  ean: string;
  onSubmit: (buyPricePence: number) => void;
  onRescan: () => void;
  submitting: boolean;
}

export function PriceEntry({ ean, onSubmit, onRescan, submitting }: PriceEntryProps) {
  const [price, setPrice] = useState("");

  function handleSubmit(e: FormEvent) {
    e.preventDefault();
    const pounds = parseFloat(price);
    if (Number.isNaN(pounds) || pounds <= 0) return;
    onSubmit(Math.round(pounds * 100));
  }

  return (
    <form onSubmit={handleSubmit}>
      <p>
        Scanned: <code>{ean}</code> <button type="button" onClick={onRescan}>Rescan</button>
      </p>
      <label htmlFor="price">Shelf price (£)</label>
      <input
        id="price"
        type="number"
        inputMode="decimal"
        step="0.01"
        min="0.01"
        value={price}
        onChange={(e) => setPrice(e.target.value)}
        autoFocus
        required
      />
      <button type="submit" disabled={submitting || !price}>
        {submitting ? "Checking…" : "Check deal"}
      </button>
    </form>
  );
}
