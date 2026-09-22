// eBay marketplace account deletion/closure notification endpoint --
// PARTNER'S keyset, kept fully separate from ebay-deletion.js.
//
// eBay's challenge request (GET ?challenge_code=...) carries no field
// identifying which app/keyset triggered it, so one endpoint URL can only
// validate against ONE verification token -- whatever every app sharing
// that URL is configured with on eBay's side. Brightside and
// gadgetcollectionns already share ebay-deletion.js's token for exactly
// that reason. A business partner's own eBay developer account needs a
// token nobody else holds, which means it needs its own endpoint URL too
// -- hence this near-duplicate file rather than a third env var on the
// shared one. See ebay-deletion.js for the full protocol notes (hash
// order, BOM gotcha, why POST just acknowledges); this file only differs
// in ENDPOINT and which env var it reads.

const crypto = require('crypto');

const ENDPOINT =
  process.env.EBAY_DELETION_ENDPOINT_PARTNER ||
  'https://www.brightsidecommerce.co.uk/api/ebay-deletion-partner';

module.exports = (req, res) => {
  const token = process.env.EBAY_VERIFICATION_TOKEN_PARTNER;

  if (req.method === 'GET') {
    if (!token) {
      res.status(500).json({ error: 'EBAY_VERIFICATION_TOKEN_PARTNER is not set' });
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
    const id =
      (req.body && (req.body.notificationId || (req.body.notification || {}).notificationId)) ||
      'unknown';
    console.log(`[ebay-deletion-partner] acknowledged notification ${id}`);
    res.status(200).end();
    return;
  }

  res.setHeader('Allow', 'GET, POST');
  res.status(405).json({ error: 'method not allowed' });
};
