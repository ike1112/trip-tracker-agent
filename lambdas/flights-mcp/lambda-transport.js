/**
 * Minimal MCP transport for a single-shot AWS Lambda invocation.
 *
 * The bundled @modelcontextprotocol/sdk transports (stdio, SSE, streamable
 * HTTP) all assume a long-lived connection where many messages flow in both
 * directions. A Lambda invocation isn't that — it's one inbound JSON-RPC
 * request and one outbound JSON-RPC response. Hosting Express + Lambda Web
 * Adapter just to satisfy the long-lived-server shape pulls ~50MB of deps
 * and a layer for no real benefit.
 *
 * This class implements just enough of the MCP transport interface for
 * `server.connect(transport)` to wire its message handler: the Lambda
 * handler calls `transport.dispatch(incoming)` with the parsed JSON-RPC
 * request and awaits the response that the server hands back via `send()`.
 */
export class LambdaTransport {
    constructor() {
        this.onmessage = undefined;   // set by Protocol.connect
        this.onerror = undefined;
        this.onclose = undefined;
        this._resolveResponse = null;
    }

    async start() { /* no persistent connection */ }
    async close() { this.onclose?.(); }

    /** Called by the MCP server with the response it wants to send. */
    async send(message) {
        if (this._resolveResponse) {
            const resolve = this._resolveResponse;
            this._resolveResponse = null;
            resolve(message);
        }
    }

    /** Feed one inbound request, get the matching response. */
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
