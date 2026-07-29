import { useEffect, useRef, useState } from "react";
import { Html5Qrcode } from "html5-qrcode";

const READER_ELEMENT_ID = "barcode-reader";

interface ScannerProps {
  onDetected: (code: string) => void;
}

// html5-qrcode, not the browser-native BarcodeDetector API -- BarcodeDetector
// has no Safari/iOS support at all (WebKit, no announced plans to add it),
// which would permanently block any iPhone user of this 3-user tool. See
// pwa/README for the tradeoff (heavier bundle, works everywhere).
export function Scanner({ onDetected }: ScannerProps) {
  const scannerRef = useRef<Html5Qrcode | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [scanning, setScanning] = useState(false);

  useEffect(() => {
    const scanner = new Html5Qrcode(READER_ELEMENT_ID);
    scannerRef.current = scanner;

    scanner
      .start(
        { facingMode: "environment" },
        { fps: 10, qrbox: { width: 250, height: 150 } },
        (decodedText) => {
          onDetected(decodedText);
        },
        () => {
          // per-frame "nothing decoded yet" callback -- expected, not an error
        },
      )
      .then(() => setScanning(true))
      .catch((err: unknown) => {
        setError(err instanceof Error ? err.message : "Could not start camera");
      });

    return () => {
      const activeScanner = scannerRef.current;
      if (!activeScanner) return;
      try {
        // html5-qrcode's stop() is typed as Promise<void> but actually
        // throws a plain synchronous string -- "Cannot stop, scanner is
        // not running or paused." -- whenever start() hasn't reached its
        // SCANNING state yet (confirmed in node_modules/html5-qrcode/esm/
        // html5-qrcode.js: the check happens before any promise is
        // constructed). A bare .catch() on the result can't catch that,
        // since the throw happens before stop() returns anything to chain
        // onto -- it crashed the whole app with no error boundary to catch
        // it (confirmed live 2026-07-29: switching tabs right after
        // landing on Scan reliably blanked the page). Needs an actual
        // try/catch around the call itself.
        activeScanner.stop().catch(() => {
          // stop() started but rejected later (e.g. already stopping) -- ignore
        });
      } catch {
        // start() never reached SCANNING (still pending, or already
        // failed/stopped) -- nothing to stop.
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div>
      <div id={READER_ELEMENT_ID} style={{ width: "100%", maxWidth: 400 }} />
      {error && (
        <p role="alert">
          Camera unavailable: {error}. Grant camera permission and reload, or enter the barcode manually below.
        </p>
      )}
      {!error && !scanning && <p>Starting camera…</p>}
    </div>
  );
}
