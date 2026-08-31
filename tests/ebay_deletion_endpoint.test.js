// Tests for landing/api/ebay-deletion.js.
//
//     node tests/ebay_deletion_endpoint.test.js
//
// Plain node, no runner, no dependencies -- this repo's suite is pytest and
// would not collect a .js file, and one function does not justify pulling a
// JavaScript toolchain into it.
//
// It lives here rather than beside the handler on purpose: Vercel turns every
// file under the project's `api/` directory into a public function, so a test
// file there would deploy as a live endpoint at /api/ebay-deletion.test. The
// landing Vercel project is rooted at `landing/`, so nothing here is deployed.

const crypto = require('crypto');

const TOKEN = 'x'.repeat(48);
const ENDPOINT = 'https://www.brightsidecommerce.co.uk/api/ebay-deletion';
process.env.EBAY_VERIFICATION_TOKEN = TOKEN;

const handler = require('../landing/api/ebay-deletion.js');

function mockRes() {
  const r = { code: null, headers: {}, body: null };
  r.status = c => { r.code = c; return r; };
  r.setHeader = (k, v) => { r.headers[k.toLowerCase()] = v; };
  r.json = o => { r.body = JSON.stringify(o); r.headers['content-type'] = r.headers['content-type'] || 'application/json'; return r; };
  r.send = s => { r.body = s; return r; };
  r.end = () => r;
  return r;
}

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log(`  ok   ${name}`); }
  else { console.log(`  FAIL ${name}${extra ? ' -- ' + extra : ''}`); failures++; }
}

// 1. challenge response matches an independently computed hash
const code = 'abc123-challenge';
let res = mockRes();
handler({ method: 'GET', query: { challenge_code: code } }, res);
const expected = crypto.createHash('sha256').update(code + TOKEN + ENDPOINT).digest('hex');
check('GET returns 200', res.code === 200, `got ${res.code}`);
check('content-type is application/json', res.headers['content-type'] === 'application/json');
check('hash matches challengeCode+token+endpoint', JSON.parse(res.body).challengeResponse === expected);
check('response body has no BOM', !res.body.startsWith('\uFEFF'));

// 2. order actually matters -- a wrong order must NOT collide
const wrongOrder = crypto.createHash('sha256').update(TOKEN + code + ENDPOINT).digest('hex');
check('hash is order-sensitive', expected !== wrongOrder);

// 3. missing challenge_code
res = mockRes();
handler({ method: 'GET', query: {} }, res);
check('GET without challenge_code is 400', res.code === 400, `got ${res.code}`);

// 4. missing token is a distinguishable 500, not a wrong hash
delete process.env.EBAY_VERIFICATION_TOKEN;
res = mockRes();
handler({ method: 'GET', query: { challenge_code: code } }, res);
check('unset token is 500', res.code === 500, `got ${res.code}`);
process.env.EBAY_VERIFICATION_TOKEN = TOKEN;

// 5. POST acknowledges, including a body-less one
res = mockRes();
handler({ method: 'POST', body: { notificationId: 'n-1' } }, res);
check('POST returns 200', res.code === 200, `got ${res.code}`);
res = mockRes();
handler({ method: 'POST' }, res);
check('POST with no body still returns 200', res.code === 200, `got ${res.code}`);

// 6. other methods
res = mockRes();
handler({ method: 'DELETE' }, res);
check('DELETE is 405', res.code === 405, `got ${res.code}`);

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
