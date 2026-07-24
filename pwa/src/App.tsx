import { useState } from "react";
import { getStoredSecret, postScan, setStoredSecret } from "./api";
import { Scanner } from "./components/Scanner";
import { PriceEntry } from "./components/PriceEntry";
import { VerdictView } from "./components/VerdictView";
import type { ApiError, ScanResponse } from "./types";

type Stage =
  | { name: "scanning" }
  | { name: "pricing"; ean: string }
  | { name: "submitting"; ean: string }
  | { name: "result"; result: ScanResponse }
  | { name: "error"; message: string; ean: string };

export function App() {
  const [hasSecret, setHasSecret] = useState(() => getStoredSecret() !== null);
  const [stage, setStage] = useState<Stage>({ name: "scanning" });

  if (!hasSecret) {
    return <SecretGate onSet={() => setHasSecret(true)} />;
  }

  if (stage.name === "scanning") {
    return (
      <div>
        <h1>FBA Scanner</h1>
        <Scanner onDetected={(ean) => setStage({ name: "pricing", ean })} />
      </div>
    );
  }

  if (stage.name === "pricing" || stage.name === "submitting") {
    return (
      <div>
        <h1>FBA Scanner</h1>
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
              if (apiErr.status === 401) setHasSecret(false);
              setStage({ name: "error", message: apiErr.message ?? "Unknown error", ean });
            }
          }}
        />
      </div>
    );
  }

  if (stage.name === "result") {
    return (
      <div>
        <h1>FBA Scanner</h1>
        <VerdictView result={stage.result} onScanAnother={() => setStage({ name: "scanning" })} />
      </div>
    );
  }

  // error
  return (
    <div>
      <h1>FBA Scanner</h1>
      <p role="alert">{stage.message}</p>
      <button type="button" onClick={() => setStage({ name: "pricing", ean: stage.ean })}>
        Try again
      </button>
      <button type="button" onClick={() => setStage({ name: "scanning" })}>
        Scan another item
      </button>
    </div>
  );
}

function SecretGate({ onSet }: { onSet: () => void }) {
  const [value, setValue] = useState("");
  return (
    <div>
      <h1>FBA Scanner</h1>
      <p>Enter the shared secret to continue. This is stored only on this device.</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          if (!value) return;
          setStoredSecret(value);
          onSet();
        }}
      >
        <input
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
