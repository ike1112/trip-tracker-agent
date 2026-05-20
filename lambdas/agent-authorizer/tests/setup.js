// Loaded via `node --import ./tests/setup.js` before any test module.
// Sets a stub COGNITO_JWKS_URL so the jwks-rsa client constructs without
// error. All real key resolution is overridden via __setSigningKeyForTests
// in the test file, so the URL is never actually fetched.
process.env.COGNITO_JWKS_URL ??= 'https://example.invalid/.well-known/jwks.json';
