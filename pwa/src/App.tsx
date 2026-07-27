import { useState } from "react";
import { getStoredSecret, postScan, setStoredSecret } from "./api";
import { Scanner } from "./components/Scanner";
import { PriceEntry } from "./components/PriceEntry";
import { VerdictView } from "./components/VerdictView";
import { ConfirmedDeals } from "./components/ConfirmedDeals";
import type { ApiError, ScanResponse } from "./types";

type Stage =
  | { name: "scanning" }
  | { name: "pricing"; ean: string }
  | { name: "submitting"; ean: string }
  | { name: "result"; result: ScanResponse }
  | { name: "error"; message: string; ean: string };

type View = "scan" | "deals";

export function App() {
  const [hasSecret, setHasSecret] = useState(() => getStoredSecret() !== null);
  const [stage, setStage] = useState<Stage>({ name: "scanning" });
  const [view, setView] = useState<View>("scan");

  if (!hasSecret) {
    return (
      <main className="app-main">
        <SecretGate onSet={() => setHasSecret(true)} />
      </main>
    );
  }

  const onAuthLost = () => setHasSecret(false);

  return (
    <>
      <header className="app-header">
        <h1>FBA Scanner</h1>
        <nav className="app-nav">
          <button type="button" className={view === "scan" ? "active" : ""} onClick={() => setView("scan")}>
            Scan
          </button>
          <button type="button" className={view === "deals" ? "active" : ""} onClick={() => setView("deals")}>
            Green Deals
          </button>
        </nav>
      </header>
      <main className="app-main">
        {view === "deals" ? (
          <ConfirmedDeals onAuthLost={onAuthLost} />
        ) : (
          <ScanFlow stage={stage} setStage={setStage} onAuthLost={onAuthLost} />
        )}
      </main>
    </>
  );
}

function ScanFlow({
  stage,
  setStage,
  onAuthLost,
}: {
  stage: Stage;
  setStage: (s: Stage) => void;
  onAuthLost: () => void;
}) {
  if (stage.name === "scanning") {
    return (
      <div className="card">
        <Scanner onDetected={(ean) => setStage({ name: "pricing", ean })} />
      </div>
    );
  }

  if (stage.name === "pricing" || stage.name === "submitting") {
    return (
      <div className="card">
        <PriceEntry
          ean={stage.ean}
          submitting={stage.name === "submitting"}
          onRescan={() => setStage({ name: "scanning" })}
          onSubmit={async (buyPricePence) => {
            const ean = stage.ean;
            setStage({ name: "submitting", ean });
            try {
              const result = await postScan(ean, buyPricePence);
              setStage({ name: "result", result });
            } catch (err) {
              const apiErr = err as ApiError;
              if (apiErr.status === 401) onAuthLost();
              setStage({ name: "error", message: apiErr.message ?? "Unknown error", ean });
            }
          }}
        />
      </div>
    );
  }

  if (stage.name === "result") {
    return (
      <div className="card">
        <VerdictView result={stage.result} onScanAnother={() => setStage({ name: "scanning" })} />
      </div>
    );
  }

  // error
  return (
    <div className="card">
      <p role="alert">{stage.message}</p>
      <button type="button" className="btn-secondary" onClick={() => setStage({ name: "pricing", ean: stage.ean })}>
        Try again
      </button>
      <button
        type="button"
        className="btn-secondary"
        style={{ marginLeft: 8 }}
        onClick={() => setStage({ name: "scanning" })}
      >
        Scan another item
      </button>
    </div>
  );
}

function SecretGate({ onSet }: { onSet: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div className="card">
      <h2>Welcome</h2>
      <p className="muted">Enter the shared secret to continue. This is stored only on this device.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!value) return;
          setStoredSecret(value);
          onSet();
        }}
      >
        <label htmlFor="secret">Shared secret</label>
        <input
          id="secret"
          type="password"
          value={value}
          onChange={(e) => setValue(e.target.value)}
          autoFocus
          required
        />
        <button type="submit">Continue</button>
      </form>
    </div>
  );
}
