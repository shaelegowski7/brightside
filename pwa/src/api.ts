import type { ApiError, ScanResponse } from "./types";

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
