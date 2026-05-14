const { Stack } = require('aws-cdk-lib');
const lambda = require('aws-cdk-lib/aws-lambda');
const FlightsMcpServerConstruct = require('./flights-mcp-server');
const HotelsMcpServerConstruct = require('./hotels-mcp-server');
const AgentConstruct = require('./agent');
const Cognito = require('./cognito');
const DataStoresConstruct = require('./data-stores');
const PollerServerConstruct = require('./poller-server');
const { NotifierServerConstruct } = require('./notifier-server');
const ObservabilityDashboardConstruct = require('./observability-dashboard');

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

        // Flights MCP server (Duffel-backed, fixture-replayable). Mode
        // comes from CDK context so deploys can pick fixture vs live
        // without code changes:  cdk deploy -c mcpMode=live -c duffelApiKey=...
        // Fixture mode is the default so a forking reviewer can deploy
        // without a Duffel account. The same MCP_MODE applies to hotels
        // too (one flag flips the whole external-API surface, intentional).
        const mcpMode = this.node.tryGetContext('mcpMode') ?? 'fixture';

        const flightsServer = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpMode,
            duffelApiKey: this.node.tryGetContext('duffelApiKey') ?? '',
        });

        // Hotels MCP server (LiteAPI-backed). Same shape as flights.
        const hotelsServer = new HotelsMcpServerConstruct(this, 'HotelsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            mcpMode,
            liteApiKey: this.node.tryGetContext('liteApiKey') ?? '',
        });

        // Trip-tracker persistence: Watches + FareHistory tables.
        // Provisioned before the agent so we can pass the table refs in for
        // env-var injection and least-privilege grants.
        const dataStores = new DataStoresConstruct(this, 'DataStoresConstruct');

        const agentConstruct = new AgentConstruct(this, 'AgentConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            flightsMcpEndpoint: flightsServer.endpoint,
            hotelsMcpEndpoint:  hotelsServer.endpoint,
            cognitoJwksUrl,
            watchesTable:     dataStores.watchesTable,
            fareHistoryTable: dataStores.fareHistoryTable,
        });

        // Trip-tracker alert notifier. Lambda that takes the poller's
        // decision output, composes a plain-text email, sends via SES,
        // and writes lastAlertedAt back to the Watches row. ADR 0005
        // documents the at-least-once semantics and price-proximity
        // dedup safety net.
        const notifierServer = new NotifierServerConstruct(this, 'NotifierServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            watchesTable:   dataStores.watchesTable,
        });

        // Trip-tracker poller. Walks every active watch on an EventBridge
        // cron, calls flights-mcp + hotels-mcp under a per-user JWT,
        // writes a FareHistory snapshot, then runs the alert gates and
        // Bedrock decision. When an alert fires, async-invokes the
        // notifier (`lambda:InvokeFunction` grant + NOTIFIER_FUNCTION_NAME
        // env wired below).
        const pollerServer = new PollerServerConstruct(this, 'PollerServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            watchesTable:     dataStores.watchesTable,
            fareHistoryTable: dataStores.fareHistoryTable,
            jwtSignatureSecret: JWT_SIGNATURE_SECRET,
            flightsMcpEndpoint: flightsServer.endpoint,
            hotelsMcpEndpoint:  hotelsServer.endpoint,
            notifierFunction: notifierServer.function,
        });

        // Observability dashboard. Instantiated last so every Lambda +
        // API Gateway ref is available. Surfaces 8 Lambda metric sources
        // (5 primary + 3 authorizer) plus 3 API Gateways plus the
        // poller's EMF counters in a single named CloudWatch dashboard.
        new ObservabilityDashboardConstruct(this, 'ObservabilityDashboard', {
            pollerFunction:            pollerServer.function,
            notifierFunction:          notifierServer.function,
            agentFunction:             agentConstruct.function,
            flightsMcpFunction:        flightsServer.function,
            hotelsMcpFunction:         hotelsServer.function,
            flightsAuthorizerFunction: flightsServer.authorizerFunction,
            hotelsAuthorizerFunction:  hotelsServer.authorizerFunction,
            agentAuthorizerFunction:   agentConstruct.authorizerFunction,
            flightsMcpApi:             flightsServer.api,
            hotelsMcpApi:              hotelsServer.api,
            agentApi:                  agentConstruct.api,
            notifierSenderEmail:       this.node.tryGetContext('notifierSenderEmail'),
        });
    }
}

module.exports = { StrandsAgentOnLambdaStack }
