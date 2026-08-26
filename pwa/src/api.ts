import { supabase } from "./supabaseClient";
import type { ApiError, ConfirmedDeal, CrawlStatus, ScanResponse } from "./types";

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

// The one place every authenticated request gets its bearer token from --
// a live Supabase session, not a stored secret (see supabaseClient.ts).
// getSession() is usually just a localStorage read (supabase-js persists
// sessions itself) but its signature is async regardless, since it may
// need a network round-trip to silently refresh an expired token.
async function authHeader(): Promise<Record<string, string>> {
  const { data } = await supabase.auth.getSession();
  if (!data.session) throw { status: 401, message: "Not signed in" } satisfies ApiError;
  return { Authorization: `Bearer ${data.session.access_token}` };
}

// 401 means the credential itself is bad/expired -- sign out so the
// AuthGate listener (see App.tsx) naturally drops back to the login
// screen. 403 means a real, currently-valid account that Brightside just
// doesn't recognise -- signing out would only re-prompt a login that
// succeeds again and hits the same wall, so leave the session alone and
// let the caller show a distinct "not authorized" message instead.
async function handleAuthFailure(status: number): Promise<ApiError> {
  if (status === 401) {
    await supabase.auth.signOut();
    return { status: 401, message: "Session expired -- please sign in again" };
  }
  return { status: 403, message: "This account isn't authorized for Brightside" };
}

export async function postScan(ean: string, buyPricePence: number): Promise<ScanResponse> {
  const headers = { "Content-Type": "application/json", ...(await authHeader()) };

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}/scan`, {
      method: "POST",
      headers,
      body: JSON.stringify({ ean, buy_price: buyPricePence }),
    });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }

  if (resp.status === 401 || resp.status === 403) throw await handleAuthFailure(resp.status);
  if (!resp.ok) {
    throw { status: resp.status, message: `Server error (${resp.status})` } satisfies ApiError;
  }
  return (await resp.json()) as ScanResponse;
}

export async function getConfirmedDeals(): Promise<ConfirmedDeal[]> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}/deals.json`, { headers: await authHeader() });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }

  if (resp.status === 401 || resp.status === 403) throw await handleAuthFailure(resp.status);
  if (!resp.ok) {
    throw { status: resp.status, message: `Server error (${resp.status})` } satisfies ApiError;
  }
  return (await resp.json()) as ConfirmedDeal[];
}

async function _crawlFetch(path: string, init?: RequestInit): Promise<CrawlStatus & { started?: boolean }> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: await authHeader() });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }
  if (resp.status === 401 || resp.status === 403) throw await handleAuthFailure(resp.status);
  if (!resp.ok) {
    throw { status: resp.status, message: `Server error (${resp.status})` } satisfies ApiError;
  }
  return await resp.json();
}

export function triggerCrawl(): Promise<CrawlStatus & { started: boolean }> {
  return _crawlFetch("/crawl", { method: "POST" }) as Promise<CrawlStatus & { started: boolean }>;
}

// Live crawl status over a WebSocket instead of polling /crawl/status on a
// timer. Browsers can't set custom headers on the native WebSocket
// handshake, so the bearer token can't ride as an Authorization header
// like every other request here -- it's sent as the first text frame
// after connecting instead of a ?token= query param, which would land in
// server/proxy logs and browser history (see app/main.py's crawl_ws
// docstring). Auto-reconnects on an unexpected close (e.g. a server
// redeploy) but not on a 4001 (bad/missing/unauthorized token), which the
// caller must re-authenticate for. Returns a cleanup function to close the
// socket and stop reconnecting.
export function connectCrawlWebSocket(
  onStatus: (status: CrawlStatus) => void,
  onError: (err: ApiError) => void,
): () => void {
  let closedByCaller = false;
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  async function connect() {
    // Fetched fresh on every connect() call, including reconnects -- not
    // once at the top -- since access tokens expire (~1h) and this socket
    // can live for hours (see CrawlPanel.tsx: connected for as long as the
    // Green Deals tab is mounted). Reusing a token captured at the first
    // connect would start failing auth on any reconnect after it expires.
    const { data } = await supabase.auth.getSession();
    if (!data.session) {
      onError({ status: 401, message: "Not signed in" });
      return;
    }
    if (closedByCaller) return;

    const wsBase = API_BASE_URL.replace(/^http/, "ws");
    socket = new WebSocket(`${wsBase}/crawl/ws`);
    const token = data.session.access_token;

    socket.onopen = () => {
      socket?.send(token);
    };
    socket.onmessage = (event) => {
      try {
        onStatus(JSON.parse(event.data as string) as CrawlStatus);
      } catch {
        // malformed frame -- drop it, the next one will self-correct
      }
    };
    socket.onclose = (event) => {
      if (closedByCaller) return;
      if (event.code === 4001) {
        supabase.auth.signOut();
        onError({ status: 401, message: "Session expired -- please sign in again" });
        return;
      }
      reconnectTimer = setTimeout(connect, 2000);
    };
  }

  connect();

  return () => {
    closedByCaller = true;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    socket?.close();
  };
}
