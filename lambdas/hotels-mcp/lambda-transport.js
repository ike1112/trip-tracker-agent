/**
 * Minimal MCP transport for a single-shot AWS Lambda invocation.
 * Identical to lambdas/flights-mcp/lambda-transport.js — duplicated rather
 * than shared to keep each MCP Lambda fully self-contained. ~25 lines, no
 * version-skew risk. If you change one, change the other (or extract to a
 * shared module when a third MCP server lands).
 *
 * See ADR 0002 for why these Lambdas are direct handlers instead of Express
 * + Lambda Web Adapter.
 */
export class LambdaTransport {
    constructor() {
        this.onmessage = undefined;
        this.onerror = undefined;
        this.onclose = undefined;
        this._resolveResponse = null;
    }

    async start() { /* no persistent connection */ }
    async close() { this.onclose?.(); }

    async send(message) {
        if (this._resolveResponse) {
            const resolve = this._resolveResponse;
            this._resolveResponse = null;
            resolve(message);
        }
    }

    dispatch(message) {
        return new Promise((resolve, reject) => {
            this._resolveResponse = resolve;
            try {
                this.onmessage?.(message);
            } catch (err) {
                reject(err);
            }
        });
    }
}
