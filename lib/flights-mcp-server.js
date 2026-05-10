const iam    = require('aws-cdk-lib/aws-iam');
const lambda = require('aws-cdk-lib/aws-lambda');
const apigw  = require('aws-cdk-lib/aws-apigateway');
const { Duration, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

/**
 * FlightsMcpServerConstruct — provisions the flights MCP Lambda
 * (Duffel-backed) + its API Gateway + JWT authorizer.
 *
 * Differences from the original McpServerConstruct (bookings-mcp):
 *
 * - No Lambda Web Adapter layer. The Lambda is a direct handler that
 *   parses the JSON-RPC body itself via a minimal in-memory MCP transport.
 *   Cuts the deploy package + cold-start cost; see ADR 0002.
 *
 * - X-Ray tracing ACTIVE — every Lambda in the request path now reports
 *   a span so the full picture (web → API GW → travel-agent → flights-mcp
 *   → Duffel) is visible end-to-end. Production-readiness companion §3.3.
 *
 * - Fixture replay mode. The MCP_MODE env var picks the live Duffel client
 *   or the fixture client at cold start. Reviewers can run the stack with
 *   MCP_MODE=fixture and no Duffel key. See ADR 0002.
 *
 * - DUFFEL_API_KEY passed through from the stack so we can rotate keys
 *   without touching this construct.
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
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret,
                // 'fixture' (default for portfolio review) or 'live'.
                MCP_MODE: props.mcpMode ?? 'fixture',
                // Only consulted when MCP_MODE=live. Empty string is fine
                // for review-mode deploys — the live client errors loudly
                // if called without a real key, which is the right behavior.
                DUFFEL_API_KEY: props.duffelApiKey ?? '',
            }
        });

        // Authorizer Lambda is reused from the existing mcp-authorizer code.
        // Same JWT secret + same `sub: travel-agent` requirement — see
        // lambdas/mcp-authorizer/index.js.
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
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret,
            },
        });

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

        return { flightsMcpEndpoint };
    }
}

module.exports = FlightsMcpServerConstruct;
