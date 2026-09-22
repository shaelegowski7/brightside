// Tests for landing/api/ebay-deletion.js and its partner-account sibling
// landing/api/ebay-deletion-partner.js. Both handlers are structurally
// identical (see ebay-deletion-partner.js's docstring for why there are
// two files rather than one endpoint serving two tokens), so one
// parametrised check runs against each.
//
//     node tests/ebay_deletion_endpoint.test.js
//
// Plain node, no runner, no dependencies -- this repo's suite is pytest and
// would not collect a .js file, and two functions do not justify pulling a
// JavaScript toolchain into it.
//
// It lives here rather than beside the handlers on purpose: Vercel turns
// every file under the project's `api/` directory into a public function,
// so a test file there would deploy as a live endpoint. The landing Vercel
// project is rooted at `landing/`, so nothing here is deployed.

const crypto = require('crypto');

let failures = 0;
function check(name, cond, extra) {
  if (cond) { console.log(`  ok   ${name}`); }
  else { console.log(`  FAIL ${name}${extra ? ' -- ' + extra : ''}`); failures++; }
}

function mockRes() {
  const r = { code: null, headers: {}, body: null };
  r.status = c => { r.code = c; return r; };
  r.setHeader = (k, v) => { r.headers[k.toLowerCase()] = v; };
  r.json = o => { r.body = JSON.stringify(o); r.headers['content-type'] = r.headers['content-type'] || 'application/json'; return r; };
  r.send = s => { r.body = s; return r; };
  r.end = () => r;
  return r;
}

function runSuite(label, handlerPath, tokenEnvName, endpoint) {
  console.log(`\n${label}`);
  const TOKEN = 'x'.repeat(48);
  process.env[tokenEnvName] = TOKEN;
  delete require.cache[require.resolve(handlerPath)];
  const handler = require(handlerPath);

  // 1. challenge response matches an independently computed hash
  const code = 'abc123-challenge';
  let res = mockRes();
  handler({ method: 'GET', query: { challenge_code: code } }, res);
  const expected = crypto.createHash('sha256').update(code + TOKEN + endpoint).digest('hex');
  check('GET returns 200', res.code === 200, `got ${res.code}`);
  check('content-type is application/json', res.headers['content-type'] === 'application/json');
  check('hash matches challengeCode+token+endpoint', JSON.parse(res.body).challengeResponse === expected);
  check('response body has no BOM', !res.body.startsWith('﻿'));

  // 2. order actually matters -- a wrong order must NOT collide
  const wrongOrder = crypto.createHash('sha256').update(TOKEN + code + endpoint).digest('hex');
  check('hash is order-sensitive', expected !== wrongOrder);

  // 3. missing challenge_code
  res = mockRes();
  handler({ method: 'GET', query: {} }, res);
  check('GET without challenge_code is 400', res.code === 400, `got ${res.code}`);

  // 4. missing token is a distinguishable 500, not a wrong hash
  delete process.env[tokenEnvName];
  res = mockRes();
  handler({ method: 'GET', query: { challenge_code: code } }, res);
  check('unset token is 500', res.code === 500, `got ${res.code}`);
  process.env[tokenEnvName] = TOKEN;

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
}

runSuite('ebay-deletion.js (Brightside / gadgetcollectionns)',
  '../landing/api/ebay-deletion.js', 'EBAY_VERIFICATION_TOKEN',
  'https://www.brightsidecommerce.co.uk/api/ebay-deletion');

runSuite('ebay-deletion-partner.js',
  '../landing/api/ebay-deletion-partner.js', 'EBAY_VERIFICATION_TOKEN_PARTNER',
  'https://www.brightsidecommerce.co.uk/api/ebay-deletion-partner');

console.log(failures ? `\n${failures} FAILED` : '\nall passed');
process.exit(failures ? 1 : 0);
