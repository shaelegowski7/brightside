import { useEffect, useState } from "react";
import type { Factor } from "@supabase/supabase-js";
import { supabase } from "./supabaseClient";
import { postScan } from "./api";
import { Scanner } from "./components/Scanner";
import { PriceEntry } from "./components/PriceEntry";
import { VerdictView } from "./components/VerdictView";
import { ConfirmedDeals } from "./components/ConfirmedDeals";
import { LoginForm } from "./components/Auth/LoginForm";
import { MfaEnroll } from "./components/Auth/MfaEnroll";
import { MfaChallenge } from "./components/Auth/MfaChallenge";
import type { ApiError, ScanResponse } from "./types";

type Stage =
  | { name: "scanning" }
  | { name: "pricing"; ean: string }
  | { name: "submitting"; ean: string }
  | { name: "result"; result: ScanResponse }
  | { name: "error"; message: string; ean: string };

type View = "scan" | "deals";

// Mandatory MFA -- no state here ever renders the app shell below aal2.
// A fresh account (zero verified TOTP factors) goes through enrollment;
// a returning account with a verified factor but an aal1 session (a new
// browser session after signing back in with just a password) goes
// through a plain challenge instead. A 403 from Brightside's own backend
// allowlist (a real-but-unrecognised account) isn't handled as a top-
// level gate state -- it can only happen after the app is already
// `ready`, and the existing per-view error handling in ConfirmedDeals/
// ScanFlow already surfaces the ApiError's message inline, which is
// enough for the rare case this actually fires (the allowlist changing
// under a live session).
type GateState =
  | { name: "checking" }
  | { name: "signed_out" }
  | { name: "needs_mfa_enrollment" }
  | { name: "needs_mfa_challenge"; factor: Factor }
  | { name: "ready" };

async function resolveGateState(): Promise<GateState> {
  const { data: sessionData } = await supabase.auth.getSession();
  if (!sessionData.session) return { name: "signed_out" };

  const { data: aalData } = await supabase.auth.mfa.getAuthenticatorAssuranceLevel();
  if (aalData?.currentLevel === "aal2") return { name: "ready" };

  const { data: factorsData } = await supabase.auth.mfa.listFactors();
  const verifiedTotp = factorsData?.totp.find((f) => f.status === "verified");
  return verifiedTotp ? { name: "needs_mfa_challenge", factor: verifiedTotp } : { name: "needs_mfa_enrollment" };
}

export function App() {
  const [gate, setGate] = useState<GateState>({ name: "checking" });
  const [stage, setStage] = useState<Stage>({ name: "scanning" });
  const [view, setView] = useState<View>("scan");

  useEffect(() => {
    resolveGateState().then(setGate);
    // Covers SIGNED_IN, SIGNED_OUT, TOKEN_REFRESHED, and MFA_CHALLENGE_VERIFIED
    // (fired internally by challengeAndVerify() once it saves the newly-
    // elevated aal2 session) -- so neither MfaEnroll nor MfaChallenge need
    // to manually flip any state on success, this listener does it.
    const { data: sub } = supabase.auth.onAuthStateChange(() => {
      resolveGateState().then(setGate);
    });
    return () => sub.subscription.unsubscribe();
  }, []);

  if (gate.name === "checking") {
    // Avoids a flash of the login form for an already-signed-in returning user.
    return (
      <main className="app-main">
        <div className="card">
          <p className="muted">Loading…</p>
        </div>
      </main>
    );
  }

  if (gate.name === "signed_out") {
    return (
      <main className="app-main">
        <LoginForm />
      </main>
    );
  }

  if (gate.name === "needs_mfa_enrollment") {
    return (
      <main className="app-main">
        <MfaEnroll />
      </main>
    );
  }

  if (gate.name === "needs_mfa_challenge") {
    return (
      <main className="app-main">
        <MfaChallenge factor={gate.factor} />
      </main>
    );
  }

  // ready (aal2) -- the app itself, unchanged from before except for
  // where auth comes from. onAuthLost is now a no-op: a 401 is already
  // handled inside api.ts (it signs the session out itself), and that
  // triggers the onAuthStateChange listener above on its own -- nothing
  // left for the callback to do, but ConfirmedDeals/CrawlPanel still call
  // it as their "stop showing stale data for this view" signal.
  const onAuthLost = () => {};

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
        <button type="button" className="btn-secondary" onClick={() => supabase.auth.signOut()}>
          Sign out
        </button>
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
