const lambda = require('aws-cdk-lib/aws-lambda');
const apigw  = require('aws-cdk-lib/aws-apigateway');
const { Duration, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

/**
 * FlightsMcpServerConstruct — provisions the flights MCP Lambda
 * (Duffel-backed) + its API Gateway + JWT authorizer.
 *
 * Design notes:
 *
 * - No Lambda Web Adapter layer. The Lambda is a direct handler that
 *   parses the JSON-RPC body itself via a minimal in-memory MCP transport.
 *   Cuts the deploy package + cold-start cost; see ADR 0002.
 *
 * - X-Ray tracing ACTIVE — every Lambda in the request path reports
 *   a span so the full picture (web → API GW → travel-agent → flights-mcp
 *   → Duffel) is visible end-to-end. Production-readiness companion §3.3.
 *
 * - Fixture replay mode. The MCP_MODE env var picks the live Duffel client
 *   or the fixture client at cold start. Reviewers can run the stack with
 *   MCP_MODE=fixture and no Duffel key. See ADR 0002.
 *
 * - DUFFEL_API_KEY passed through from the stack so we can rotate keys
 *   without touching this construct.
 *
 * - Exposes four properties on `this` for downstream consumers (stack
 *   wiring, dashboard widgets): `endpoint` (string URL), `function`
 *   (the MCP Lambda), `api` (the API Gateway RestApi), and
 *   `authorizerFunction` (the JWT authorizer Lambda). No explicit
 *   `return` from the constructor — an object return would replace
 *   the instance per ES spec and make these properties unreachable.
 */
class FlightsMcpServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // Lambda — direct handler, no LWA, no Express, no log4js.
        const flightsMcpFn = new lambda.Function(this, 'FlightsMcpServer', {
            functionName: 'flights-mcp-server',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'index.handler',
            timeout: Duration.seconds(15),
            memorySize: 512,
            tracing: lambda.Tracing.ACTIVE,
            code: lambda.Code.fromAsset('./lambdas/flights-mcp', {
                // node_modules is large — exclude dev junk and the test
                // dir from the deploy package.
                exclude: ['tests/**', '.nyc_output/**', '*.log', '.git/**']
            }),
            environment: {
                // The server handler re-verifies the bearer JWT in-handler
                // as defense in depth (ADR 0006), so it needs both signing
                // secrets — agent-minted and poller-minted tokens both
                // reach this Lambda.
                AGENT_JWT_SECRET_ARN:  props.agentJwtSecret.secretArn,
                POLLER_JWT_SECRET_ARN: props.pollerJwtSecret.secretArn,
                // 'fixture' (default for portfolio review) or 'live'.
                MCP_MODE: props.mcpMode ?? 'fixture',
                // Only consulted when MCP_MODE=live. Empty string is fine
                // for review-mode deploys — the live client errors loudly
                // if called without a real key, which is the right behavior.
                DUFFEL_API_KEY: props.duffelApiKey ?? '',
            }
        });

        // Authorizer Lambda is reused from the existing mcp-authorizer code.
        // It verifies against both per-component secrets, coupling each
        // secret to its allowed sub (travel-agent vs trip-tracker-poller)
        // — see lambdas/mcp-authorizer/index.js (ADR 0006).
        const flightsAuthorizerFn = new lambda.Function(this, 'FlightsMcpAuthorizerFn', {
            functionName: 'flights-mcp-server-authorizer',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'index.handler',
            timeout: Duration.seconds(10),
            memorySize: 256,
            tracing: lambda.Tracing.ACTIVE,
            code: lambda.Code.fromAsset('./lambdas/mcp-authorizer'),
            environment: {
                AGENT_JWT_SECRET_ARN:  props.agentJwtSecret.secretArn,
                POLLER_JWT_SECRET_ARN: props.pollerJwtSecret.secretArn,
            },
        });

        // Least-privilege read: both verifier Lambdas (server handler +
        // authorizer) read both secrets; never Resource: '*'.
        props.agentJwtSecret.grantRead(flightsMcpFn);
        props.pollerJwtSecret.grantRead(flightsMcpFn);
        props.agentJwtSecret.grantRead(flightsAuthorizerFn);
        props.pollerJwtSecret.grantRead(flightsAuthorizerFn);

        // API Gateway in front of the flights-mcp Lambda. Same shape as
        // the existing McpServerConstruct: regional REST, custom Token
        // authorizer on /mcp, ANY method (MCP semantics live in the body).
        const flightsApi = new apigw.RestApi(this, 'FlightsMcpApi', {
            restApiName: 'flights-mcp-api',
            endpointTypes: [apigw.EndpointType.REGIONAL],
            deploy: true,
        });

        const mcpResource = flightsApi.root.addResource('mcp');

        const authorizer = new apigw.TokenAuthorizer(this, 'FlightsMcpAuthorizer', {
            handler: flightsAuthorizerFn,
            identitySource: apigw.IdentitySource.header('Authorization'),
        });

        mcpResource.addMethod('ANY', new apigw.LambdaIntegration(flightsMcpFn), {
            authorizer,
            authorizationType: apigw.AuthorizationType.CUSTOM,
        });

        // Endpoint exposed to the stack so the agent Lambda can be told
        // where to send flights-tool requests.
        const flightsMcpEndpoint = `${flightsApi.url}mcp`;

        new CfnOutput(this, 'FlightsMcpEndpoint', { value: flightsMcpEndpoint });
        new CfnOutput(this, 'FlightsMcpMode',     { value: props.mcpMode ?? 'fixture' });

        this.endpoint           = flightsMcpEndpoint;
        this.function           = flightsMcpFn;
        this.api                = flightsApi;
        this.authorizerFunction = flightsAuthorizerFn;
    }
}

module.exports = FlightsMcpServerConstruct;
