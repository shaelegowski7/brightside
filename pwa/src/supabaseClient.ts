import { createClient } from "@supabase/supabase-js";

const supabaseUrl = import.meta.env.VITE_SUPABASE_URL as string;
const supabaseAnonKey = import.meta.env.VITE_SUPABASE_ANON_KEY as string;

if (!supabaseUrl || !supabaseAnonKey) {
  // eslint-disable-next-line no-console
  console.warn("VITE_SUPABASE_URL/VITE_SUPABASE_ANON_KEY missing -- auth will not work until pwa/.env.local is set.");
}

// Both externalized via env (unlike sentimentfx's supabaseClient.js, which
// hardcodes its project URL) -- this project's own convention already
// externalizes everything (see VITE_API_BASE_URL), and there's no
// sensitivity cost: the anon key is meant to be public, and this app
// stores no data in Supabase's own Postgres (auth.users only, see
// app/auth.py) so there are no RLS policies riding on it being secret.
export const supabase = createClient(
  supabaseUrl || "https://placeholder.supabase.co",
  supabaseAnonKey || "placeholder-anon-key",
);
