import type { ApiError, ConfirmedDeal, CrawlStatus, ScanResponse } from "./types";

const SECRET_STORAGE_KEY = "fba-scanner-shared-secret";
const API_BASE_URL = import.meta.env.VITE_API_BASE_URL as string;

// Prompted once, kept in localStorage -- deliberately NOT baked into the
// build via import.meta.env, which would inline it into the publicly-
// fetchable JS bundle. Same effort, meaningfully less exposed, and
// rotating the secret doesn't need a redeploy.
export function getStoredSecret(): string | null {
  return localStorage.getItem(SECRET_STORAGE_KEY);
}

export function setStoredSecret(secret: string): void {
  localStorage.setItem(SECRET_STORAGE_KEY, secret);
}

export function clearStoredSecret(): void {
  localStorage.removeItem(SECRET_STORAGE_KEY);
}

export async function postScan(ean: string, buyPricePence: number): Promise<ScanResponse> {
  const secret = getStoredSecret();
  if (!secret) {
    throw { status: 401, message: "No shared secret set" } satisfies ApiError;
  }

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}/scan`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Shared-Secret": secret,
      },
      body: JSON.stringify({ ean, buy_price: buyPricePence }),
    });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }

  if (resp.status === 401) {
    clearStoredSecret();
    throw { status: 401, message: "Shared secret rejected -- re-enter it" } satisfies ApiError;
  }
  if (!resp.ok) {
    throw { status: resp.status, message: `Server error (${resp.status})` } satisfies ApiError;
  }
  return (await resp.json()) as ScanResponse;
}

export async function getConfirmedDeals(): Promise<ConfirmedDeal[]> {
  const secret = getStoredSecret();
  if (!secret) {
    throw { status: 401, message: "No shared secret set" } satisfies ApiError;
  }

  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}/deals.json`, {
      headers: { "X-Shared-Secret": secret },
    });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }

  if (resp.status === 401) {
    clearStoredSecret();
    throw { status: 401, message: "Shared secret rejected -- re-enter it" } satisfies ApiError;
  }
  if (!resp.ok) {
    throw { status: resp.status, message: `Server error (${resp.status})` } satisfies ApiError;
  }
  return (await resp.json()) as ConfirmedDeal[];
}

function authHeader(): Record<string, string> {
  const secret = getStoredSecret();
  if (!secret) throw { status: 401, message: "No shared secret set" } satisfies ApiError;
  return { "X-Shared-Secret": secret };
}

async function _crawlFetch(path: string, init?: RequestInit): Promise<CrawlStatus & { started?: boolean }> {
  let resp: Response;
  try {
    resp = await fetch(`${API_BASE_URL}${path}`, { ...init, headers: authHeader() });
  } catch {
    throw { status: 0, message: "Network error -- check your connection" } satisfies ApiError;
  }
  if (resp.status === 401) {
    clearStoredSecret();
    throw { status: 401, message: "Shared secret rejected -- re-enter it" } satisfies ApiError;
  }
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
// handshake, so the shared secret can't ride as X-Shared-Secret like every
// other request here -- it's sent as the first text frame after connecting
// instead of a ?secret= query param, which would land in server/proxy logs
// and browser history (see app/main.py's crawl_ws docstring). Auto-
// reconnects on an unexpected close (e.g. a server redeploy) but not on a
// 4001 (bad/missing secret), which the caller must re-prompt for. Returns
// a cleanup function to close the socket and stop reconnecting.
export function connectCrawlWebSocket(
  onStatus: (status: CrawlStatus) => void,
  onError: (err: ApiError) => void,
): () => void {
  const secret = getStoredSecret();
  if (!secret) {
    onError({ status: 401, message: "No shared secret set" });
    return () => {};
  }

  let closedByCaller = false;
  let socket: WebSocket | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;

  function connect() {
    const wsBase = API_BASE_URL.replace(/^http/, "ws");
    socket = new WebSocket(`${wsBase}/crawl/ws`);

    socket.onopen = () => {
      socket?.send(secret as string);
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
        clearStoredSecret();
        onError({ status: 401, message: "Shared secret rejected -- re-enter it" });
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
