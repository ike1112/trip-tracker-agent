const { Stack } = require('aws-cdk-lib');
const lambda = require('aws-cdk-lib/aws-lambda');
const McpServerConstruct = require('./mcp-server');
const FlightsMcpServerConstruct = require('./flights-mcp-server');
const HotelsMcpServerConstruct = require('./hotels-mcp-server');
const AgentConstruct = require('./agent');
const Cognito = require('./cognito');
const DataStoresConstruct = require('./data-stores');
const PollerServerConstruct = require('./poller-server');

// The IaC below uses Arm64 by default.
// Change to x86 if you're building on x86 arch.
const FN_ARCHITECTURE = lambda.Architecture.ARM_64;
const JWT_SIGNATURE_SECRET = 'jwt-signature-secret';

class StrandsAgentOnLambdaStack extends Stack {
    constructor(scope, id, props) {
        super(scope, id, props);

        const {
            cognitoJwksUrl
        } = new Cognito(this, 'Cognito');

        const {
            mcpEndpoint
        } = new McpServerConstruct(this, 'McpServerConstruct',{
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET
        });

        // Slice 3 — flights MCP server (Duffel-backed, fixture-replayable).
        // Mode comes from CDK context so deploys can pick fixture vs live
        // without code changes:  cdk deploy -c mcpMode=live -c duffelApiKey=...
        // Fixture mode is the default so a forking reviewer can deploy
        // without a Duffel account. The same MCP_MODE applies to hotels too
        // (one flag flips the whole external-API surface, intentional).
        const mcpMode = this.node.tryGetContext('mcpMode') ?? 'fixture';

        const {
            flightsMcpEndpoint
        } = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpMode,
            duffelApiKey: this.node.tryGetContext('duffelApiKey') ?? '',
        });

        // Slice 4 — hotels MCP server (LiteAPI-backed). Same shape as flights.
        const {
            hotelsMcpEndpoint
        } = new HotelsMcpServerConstruct(this, 'HotelsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpMode,
            liteApiKey: this.node.tryGetContext('liteApiKey') ?? '',
        });

        // Trip-tracker persistence: Watches + FareHistory tables.
        // Provisioned before the agent so we can pass the table refs in for
        // env-var injection and least-privilege grants.
        const dataStores = new DataStoresConstruct(this, 'DataStoresConstruct');

        new AgentConstruct(this, 'AgentConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpEndpoint,
            flightsMcpEndpoint,
            hotelsMcpEndpoint,
            cognitoJwksUrl,
            watchesTable:     dataStores.watchesTable,
            fareHistoryTable: dataStores.fareHistoryTable,
        });

        // Slice 5 — trip-tracker poller. Walks every active watch on an
        // EventBridge cron, calls flights-mcp + hotels-mcp, writes a
        // FareHistory snapshot, runs the alert gates. Task 1 lands the
        // Lambda with the schedule disabled; T5 enables it. T2 added the
        // MCP-call wiring (JWT secret + endpoints).
        new PollerServerConstruct(this, 'PollerServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            watchesTable:     dataStores.watchesTable,
            fareHistoryTable: dataStores.fareHistoryTable,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            flightsMcpEndpoint,
            hotelsMcpEndpoint,
        });

    }
}

module.exports = { StrandsAgentOnLambdaStack }
