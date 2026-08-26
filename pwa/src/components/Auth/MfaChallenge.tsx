import { useState } from "react";
import { supabase } from "../../supabaseClient";
import type { Factor } from "@supabase/supabase-js";

export function MfaChallenge({ factor }: { factor: Factor }) {
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    setVerifying(true);
    setError(null);
    const { error: verifyError } = await supabase.auth.mfa.challengeAndVerify({
      factorId: factor.id,
      code,
    });
    if (verifyError) {
      setError(verifyError.message);
      setVerifying(false);
      return;
    }
    // Same as MfaEnroll -- the onAuthStateChange listener in App.tsx picks
    // up the newly-elevated aal2 session on its own.
  }

  return (
    <div className="card">
      <h2>Enter your code</h2>
      <p className="muted">Open your authenticator app and enter the current 6-digit code.</p>
      {error && <p role="alert">{error}</p>}
      <form onSubmit={handleVerify}>
        <label htmlFor="mfa-challenge-code">6-digit code</label>
        <input
          id="mfa-challenge-code"
          type="text"
          inputMode="numeric"
          pattern="[0-9]{6}"
          maxLength={6}
          value={code}
          onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))}
          autoFocus
          required
        />
        <button type="submit" disabled={verifying || code.length !== 6}>
          {verifying ? "Verifying…" : "Verify"}
        </button>
      </form>
      <button
        type="button"
        className="btn-secondary"
        style={{ marginTop: 12 }}
        onClick={() => supabase.auth.signOut()}
      >
        Sign out
      </button>
    </div>
  );
}
