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
      if (scannerRef.current) {
        scannerRef.current.stop().catch(() => {
          // already stopped/stopping -- ignore
        });
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
