// Loaded via `node --import ./tests/setup.js` before any test module.
// The two-secret verifier reads these ARNs from the environment; the
// tests seed the in-memory secret cache (via __seedSecretCacheForTests)
// keyed by exactly these ARN strings, so no AWS SDK call ever fires.
process.env.AGENT_JWT_SECRET_ARN ??= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:agent-test';
process.env.POLLER_JWT_SECRET_ARN ??= 'arn:aws:secretsmanager:us-east-1:000000000000:secret:poller-test';
