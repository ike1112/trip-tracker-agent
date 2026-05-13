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
// TODO(ADR 0006): externalise this to AWS Secrets Manager and rotate
// per-component. The single shared literal makes every Lambda in the
// chain mintable from anywhere with repo access. Threat model
// docs/threat-model.md row "JWT_SIGNATURE_SECRET" already documents
// the risk; do not deploy to a shared environment until this is resolved.
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

        // Flights MCP server (Duffel-backed, fixture-replayable). Mode
        // comes from CDK context so deploys can pick fixture vs live
        // without code changes:  cdk deploy -c mcpMode=live -c duffelApiKey=...
        // Fixture mode is the default so a forking reviewer can deploy
        // without a Duffel account. The same MCP_MODE applies to hotels
        // too (one flag flips the whole external-API surface, intentional).
        const mcpMode = this.node.tryGetContext('mcpMode') ?? 'fixture';

        const {
            flightsMcpEndpoint
        } = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpMode,
            duffelApiKey: this.node.tryGetContext('duffelApiKey') ?? '',
        });

        // Hotels MCP server (LiteAPI-backed). Same shape as flights.
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

        // Trip-tracker poller. Walks every active watch on an EventBridge
        // cron, calls flights-mcp + hotels-mcp under a per-user JWT,
        // writes a FareHistory snapshot, then runs the alert gates and
        // Bedrock decision.
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
