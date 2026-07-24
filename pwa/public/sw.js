// Minimal no-op service worker -- exists only to satisfy PWA installability
// heuristics (a registered service worker is required by most browsers'
// "Add to Home Screen" criteria). No caching: this tool is inherently
// network-dependent (every scan needs a live round-trip to score a deal),
// so an offline shell would be misleading, not useful. See pwa/README for
// the "what's necessary vs. gold-plating" reasoning.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (event) => event.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {
  // Intentionally pass-through -- no caching layer.
});
