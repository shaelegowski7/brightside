import { useEffect, useState } from "react";
import { supabase } from "../../supabaseClient";

interface EnrollData {
  factorId: string;
  qrCodeSvg: string;
  manualSecret: string;
}

export function MfaEnroll() {
  const [enrollData, setEnrollData] = useState<EnrollData | null>(null);
  const [code, setCode] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [verifying, setVerifying] = useState(false);

  useEffect(() => {
    let cancelled = false;
    supabase.auth.mfa.enroll({ factorType: "totp" }).then(({ data, error: enrollError }) => {
      if (cancelled) return;
      if (enrollError) {
        setError(enrollError.message);
        return;
      }
      setEnrollData({
        factorId: data.id,
        // The installed @supabase/auth-js already returns qr_code as a full
        // `data:image/svg+xml;utf-8,...` URI (see GoTrueClient.ts mfa.enroll) --
        // wrapping it again here double-encodes it into a broken image.
        qrCodeSvg: data.totp.qr_code,
        manualSecret: data.totp.secret,
      });
    });
    return () => {
      cancelled = true;
    };
  }, []);

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    if (!enrollData) return;
    setVerifying(true);
    setError(null);
    const { error: verifyError } = await supabase.auth.mfa.challengeAndVerify({
      factorId: enrollData.factorId,
      code,
    });
    if (verifyError) {
      setError(verifyError.message);
      setVerifying(false);
      return;
    }
    // On success there's nothing else to do -- verifying elevates the
    // session to aal2 and App.tsx's onAuthStateChange listener picks that
    // up and moves the gate on to the app itself.
  }

  if (!enrollData) {
    return (
      <div className="card">
        <h2>Set up two-factor authentication</h2>
        {error ? <p role="alert">{error}</p> : <p className="muted">Preparing enrollment…</p>}
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Set up two-factor authentication</h2>
      <p className="muted">
        Required for every Brightside account. Scan this with an authenticator app (Google Authenticator, Authy,
        1Password, etc.), then enter the 6-digit code it shows.
      </p>
      {error && <p role="alert">{error}</p>}
      <img
        src={enrollData.qrCodeSvg}
        alt="Scan with your authenticator app"
        style={{ width: 180, height: 180, display: "block", margin: "12px auto", background: "#fff", borderRadius: 8, padding: 8 }}
      />
      <p className="muted">
        Can't scan? Enter this code manually: <code>{enrollData.manualSecret}</code>
      </p>
      <form onSubmit={handleVerify}>
        <label htmlFor="mfa-code">6-digit code</label>
        <input
          id="mfa-code"
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
          {verifying ? "Verifying…" : "Verify and continue"}
        </button>
      </form>
    </div>
  );
}
