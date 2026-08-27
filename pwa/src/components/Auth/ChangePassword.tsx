import { useState } from "react";
import { supabase } from "../../supabaseClient";

// The user already holds a live aal2 session at this point (this only
// renders inside the app shell), so unlike LoginForm there's no separate
// identity check needed -- updateUser() applies directly against the
// current session.
function friendlyError(message: string): string {
  if (/at least \d+ characters/i.test(message)) return message;
  if (/rate limit/i.test(message)) return "Too many attempts -- wait a moment and try again.";
  return message;
}

export function ChangePassword({ onDone, forced = false }: { onDone: () => void; forced?: boolean }) {
  const [newPassword, setNewPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (newPassword !== confirmPassword) {
      setError("Passwords don't match.");
      return;
    }
    setSubmitting(true);
    setError(null);
    // password_changed_at drives both the PWA's own gate (App.tsx) and the
    // backend's per-request check (app/auth.py) -- Supabase merges `data`
    // into the existing user_metadata rather than replacing it.
    const { error: updateError } = await supabase.auth.updateUser({
      password: newPassword,
      data: { password_changed_at: new Date().toISOString() },
    });
    setSubmitting(false);
    if (updateError) {
      setError(friendlyError(updateError.message));
      return;
    }
    setDone(true);
  }

  if (done) {
    return (
      <div className="card">
        <h2>Password changed</h2>
        {forced ? (
          // App.tsx's onAuthStateChange listener picks up the metadata
          // update on its own and swaps this out for the app shell --
          // nothing for the user to click here, unlike the voluntary path.
          <p className="muted">One moment…</p>
        ) : (
          <>
            <p className="muted">Your password has been updated.</p>
            <button type="button" className="btn-secondary" onClick={onDone}>
              Done
            </button>
          </>
        )}
      </div>
    );
  }

  return (
    <div className="card">
      <h2>Change password</h2>
      {forced && (
        <p className="muted">
          Your password is over a year old -- Brightside requires a change every 365 days. Set a new one to continue.
        </p>
      )}
      {error && <p role="alert">{error}</p>}
      <form onSubmit={handleSubmit}>
        <label htmlFor="new-password">New password</label>
        <input
          id="new-password"
          type="password"
          value={newPassword}
          onChange={(e) => setNewPassword(e.target.value)}
          autoComplete="new-password"
          autoFocus
          required
          minLength={6}
        />
        <label htmlFor="confirm-password" style={{ marginTop: 12 }}>
          Confirm new password
        </label>
        <input
          id="confirm-password"
          type="password"
          value={confirmPassword}
          onChange={(e) => setConfirmPassword(e.target.value)}
          autoComplete="new-password"
          required
          minLength={6}
        />
        <button type="submit" disabled={submitting}>
          {submitting ? "Updating…" : "Update password"}
        </button>
      </form>
      {!forced && (
        <button type="button" className="btn-secondary" style={{ marginTop: 12 }} onClick={onDone}>
          Cancel
        </button>
      )}
    </div>
  );
}
