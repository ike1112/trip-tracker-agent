// Loaded via `node --import ./tests/setup.js` before any test module.
// ESM hoists imports, so setting env vars at the top of a test file is too
// late — client.js has already read MCP_MODE by the time the body runs.
process.env.MCP_MODE = 'fixture';
process.env.JWT_SIGNATURE_SECRET ??= 'test-secret';
