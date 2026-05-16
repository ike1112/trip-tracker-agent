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
const SecretsConstruct = require('./secrets');
const { BudgetAlarmConstruct } = require('./budget-alarm');

// The IaC below uses Arm64 by default.
// Change to x86 if you're building on x86 arch.
const FN_ARCHITECTURE = lambda.Architecture.ARM_64;

class StrandsAgentOnLambdaStack extends Stack {
    constructor(scope, id, props) {
        super(scope, id, props);

        const {
            cognitoJwksUrl
        } = new Cognito(this, 'Cognito');

        // Per-component JWT signing secrets (ADR 0006). Created before any
        // consumer so the agent / poller / MCP-server constructs can take
        // the Secret instances as props and scope their own grantRead.
        const secrets = new SecretsConstruct(this, 'SecretsConstruct');

        // Account-level $10/mo cost budget with email alerts — cheap
        // insurance against a runaway poll loop (design-spec §300).
        // Reads its email from context; depends on nothing, so no props.
        new BudgetAlarmConstruct(this, 'BudgetAlarmConstruct');

        // Flights MCP server (Duffel-backed, fixture-replayable). Mode
        // comes from CDK context so deploys can pick fixture vs live
        // without code changes:  cdk deploy -c mcpMode=live -c duffelApiKey=...
        // Fixture mode is the default so a forking reviewer can deploy
        // without a Duffel account. The same MCP_MODE applies to hotels
        // too (one flag flips the whole external-API surface, intentional).
        const mcpMode = this.node.tryGetContext('mcpMode') ?? 'fixture';

        const flightsServer = new FlightsMcpServerConstruct(this, 'FlightsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            agentJwtSecret:  secrets.agentJwtSecret,
            pollerJwtSecret: secrets.pollerJwtSecret,
            mcpMode,
            duffelApiKey: this.node.tryGetContext('duffelApiKey') ?? '',
        });

        // Hotels MCP server (LiteAPI-backed). Same shape as flights.
        const hotelsServer = new HotelsMcpServerConstruct(this, 'HotelsMcpServerConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            agentJwtSecret:  secrets.agentJwtSecret,
            pollerJwtSecret: secrets.pollerJwtSecret,
            mcpMode,
            liteApiKey: this.node.tryGetContext('liteApiKey') ?? '',
        });

        // Trip-tracker persistence: Watches + FareHistory tables.
        // Provisioned before the agent so we can pass the table refs in for
        // env-var injection and least-privilege grants.
        const dataStores = new DataStoresConstruct(this, 'DataStoresConstruct');

        const agentConstruct = new AgentConstruct(this, 'AgentConstruct', {
            fnArchitecture: FN_ARCHITECTURE,
            agentJwtSecret: secrets.agentJwtSecret,
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
            pollerJwtSecret: secrets.pollerJwtSecret,
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
