// eBay marketplace account deletion/closure notification endpoint.
//
// eBay requires every developer to either subscribe to these notifications
// or formally opt out, and will not activate a production keyset until one
// of the two is done. We subscribe: the opt-out is a standing claim that we
// persist no eBay data, which stops being true the first time Brightside
// reads an order and sees a buyer's name and address.
//
// This lives on the landing site rather than the FastAPI app because the
// landing site is the only part of Brightside with a public HTTPS address.
// It is deliberately dependency-free and stateless.
//
// Two methods, both required by eBay -- the subscription is rejected if
// either is missing:
//
//   GET  ?challenge_code=...  -> 200 {"challengeResponse": "<sha256 hex>"}
//   POST <notification body>  -> 200, empty
//
// Verified against eBay's own spec 2026-08-31
// (developer.ebay.com/develop/guides/sell/marketplace-user-account-deletion):
//   - the hash is sha256 over challengeCode + verificationToken + endpoint,
//     concatenated IN THAT ORDER; any other order fails validation
//   - `endpoint` is the URL string exactly as typed into the eBay portal,
//     which is why it is a constant here and not rebuilt from the request.
//     Reconstructing it from Host/X-Forwarded-Proto looks equivalent and is
//     not: every preview deployment would hash its own hostname, and the
//     apex domain 308-redirects to www, so a request can arrive with a Host
//     that was never the registered endpoint.
//   - the response must be application/json. eBay explicitly warns that
//     hand-built response strings tend to carry a BOM, which fails their
//     JSON parse -- hence JSON.stringify and an explicit content-type.

const crypto = require('crypto');

const ENDPOINT =
  process.env.EBAY_DELETION_ENDPOINT ||
  'https://www.brightsidecommerce.co.uk/api/ebay-deletion';

module.exports = (req, res) => {
  const token = process.env.EBAY_VERIFICATION_TOKEN;

  if (req.method === 'GET') {
    // Missing config is a 500, not a wrong hash: a wrong hash reads as "eBay
    // and this endpoint disagree" and sends you hunting the hash order,
    // whereas this says plainly that the env var never got set.
    if (!token) {
      res.status(500).json({ error: 'EBAY_VERIFICATION_TOKEN is not set' });
      return;
    }

    const challengeCode = req.query && req.query.challenge_code;
    if (!challengeCode) {
      res.status(400).json({ error: 'challenge_code query parameter is required' });
      return;
    }

    const hash = crypto.createHash('sha256');
    hash.update(challengeCode);
    hash.update(token);
    hash.update(ENDPOINT);

    res.setHeader('Content-Type', 'application/json');
    res.status(200).send(JSON.stringify({ challengeResponse: hash.digest('hex') }));
    return;
  }

  if (req.method === 'POST') {
    // A real deletion notice. Brightside stores no eBay user data -- the
    // ebay_listings table holds our own SKUs plus eBay's identifiers for our
    // own listings -- so there is nothing to erase and acknowledging is the
    // complete correct response.
    //
    // Acknowledge whatever arrives. eBay retries anything that is not 2xx and
    // marks the endpoint down after repeated failures, which unsubscribes the
    // keyset; a 200 here is worth more than strict validation of a payload we
    // do not act on.
    //
    // Not doing: verifying the x-ebay-signature header (ECDSA against eBay's
    // rotating public keys, fetched from the Notification API). eBay
    // recommends it and does not require it. It buys nothing while the body
    // is unused -- a forged notification would make us do exactly what a real
    // one does, i.e. nothing -- but it becomes necessary the moment this
    // handler starts deleting rows.
    const id =
      (req.body && (req.body.notificationId || (req.body.notification || {}).notificationId)) ||
      'unknown';
    console.log(`[ebay-deletion] acknowledged notification ${id}`);
    res.status(200).end();
    return;
  }

  res.setHeader('Allow', 'GET, POST');
  res.status(405).json({ error: 'method not allowed' });
};
