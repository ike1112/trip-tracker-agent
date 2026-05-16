// Loaded via `node --import ./tests/setup.js` before any test module.
// ESM hoists imports, so setting env vars at the top of a test file is too
// late — client.js has already read MCP_MODE by the time the body runs.
process.env.MCP_MODE = 'fixture';
// The in-handler verifier reads these ARNs and looks up the secret via
// getSecret(); handler.test.js seeds the in-memory cache under exactly
// these keys (__seedSecretCacheForTests) so no AWS SDK call ever fires.
process.env.AGENT_JWT_SECRET_ARN ??= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:agent-test';
process.env.POLLER_JWT_SECRET_ARN ??= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:poller-test';
