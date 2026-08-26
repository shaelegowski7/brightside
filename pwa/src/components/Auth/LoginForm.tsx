import { useState } from "react";
import { supabase } from "../../supabaseClient";

// No signup form -- accounts are admin-provisioned directly in the
// Supabase dashboard for the small, known set of real users (see
// SECURITY.md's rollout notes), not self-serve. Public signup is disabled
// on the Supabase project itself; this form only ever signs an existing
// account in.
function friendlyError(message: string): string {
  if (/invalid login credentials/i.test(message)) return "Incorrect email or password.";
  if (/rate limit/i.test(message)) return "Too many attempts -- wait a moment and try again.";
  return message;
}

export function LoginForm() {
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [resetSent, setResetSent] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    const { error: signInError } = await supabase.auth.signInWithPassword({ email, password });
    if (signInError) setError(friendlyError(signInError.message));
    setSubmitting(false);
    // On success there's nothing else to do here -- App.tsx's top-level
    // onAuthStateChange listener picks up the new session and moves the
    // gate on to the MFA step.
  }

  async function handleForgotPassword() {
    if (!email) {
      setError("Enter your email above first, then tap \"Forgot password?\"");
      return;
    }
    setError(null);
    const { error: resetError } = await supabase.auth.resetPasswordForEmail(email, {
      redirectTo: window.location.href,
    });
    if (resetError) setError(friendlyError(resetError.message));
    else setResetSent(true);
  }

  return (
    <div className="card">
      <h2>Sign in</h2>
      <p className="muted">Brightside is invite-only -- accounts are set up by the team, not self-serve.</p>
      {error && <p role="alert">{error}</p>}
      {resetSent && <p className="muted">Password reset email sent -- check your inbox.</p>}
      <form onSubmit={handleSubmit}>
        <label htmlFor="email">Email</label>
        <input
          id="email"
          type="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          autoComplete="username"
          autoFocus
          required
        />
        <label htmlFor="password" style={{ marginTop: 12 }}>Password</label>
        <input
          id="password"
          type="password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          autoComplete="current-password"
          required
        />
        <button type="submit" disabled={submitting}>
          {submitting ? "Signing in…" : "Sign in"}
        </button>
      </form>
      <button type="button" className="btn-secondary" style={{ marginTop: 12 }} onClick={handleForgotPassword}>
        Forgot password?
      </button>
    </div>
  );
}
