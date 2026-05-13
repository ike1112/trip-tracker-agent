const lambda = require('aws-cdk-lib/aws-lambda');
const apigw = require('aws-cdk-lib/aws-apigateway');
const { Stack, Duration, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

// Provisions the MCP domain used by the travel-agent Lambda:
// - MCP application Lambda (bookings tools)
// - API Gateway endpoint (/mcp)
// - Custom Lambda authorizer for bearer-token validation
//
// Design intent:
// Keep MCP tool execution in a separate service boundary from the agent logic.
// This allows independent deployment/scaling and enforces auth at the API edge.
class McpServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // Lambda Web Adapter layer allows running an Express HTTP app on Lambda.
        // The ARNs are region-scoped and architecture-specific (Arm64 in this stack).
        const lwaLayerArn = `arn:aws:lambda:${Stack.of(this).region}:753240598075:layer:LambdaAdapterLayerArm64:25`;
        const lwaLayer = lambda.LayerVersion.fromLayerVersionArn(this, 'LWALayer', lwaLayerArn);

        // MCP server runtime (bookings-mcp). Uses run.sh as bootstrap to start
        // the Node/Express app that serves MCP over HTTP.
        const bookingsMcpServerFn = new lambda.Function(this, 'BookingsMcpServer', {
            functionName: 'bookings-mcp-server',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'run.sh',
            timeout: Duration.seconds(10),
            memorySize: 1024,
            tracing: lambda.Tracing.ACTIVE,   // X-Ray across every Lambda in the request path
            code: lambda.Code.fromAsset('./lambdas/bookings-mcp'),
            layers: [lwaLayer],
            environment: {
                // Required by Lambda Web Adapter:
                // - execute wrapper swaps Lambda bootstrap
                // - app listens on AWS_LWA_PORT
                AWS_LAMBDA_EXEC_WRAPPER: '/opt/bootstrap',
                AWS_LWA_PORT: "3001",
                // Shared secret used by MCP app + authorizer to validate internal JWTs.
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret
            }
        });

        // Public API front door for the MCP server.
        // Regional endpoint keeps traffic in-region and minimizes latency.
        const mcpApi = new apigw.RestApi(this, 'McpApi', {
            restApiName: 'travel-agent-mcp-api',
            endpointTypes: [apigw.EndpointType.REGIONAL],
            deploy: true
        });

        // MCP protocol endpoint path: /mcp
        const mcpResource = mcpApi.root.addResource('mcp');

        // Edge authorizer for API Gateway. This blocks unauthorized traffic
        // before invoking the MCP app Lambda (cost + security benefit).
        const mcpAuthorizerFn = new lambda.Function(this, 'McpAuthorizerFn', {
            functionName: 'bookings-mcp-server-authorizer',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'index.handler',
            timeout: Duration.seconds(10),
            memorySize: 1024,
            tracing: lambda.Tracing.ACTIVE,
            code: lambda.Code.fromAsset('./lambdas/mcp-authorizer'),
            environment: {
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret
            }
        });

        // Token authorizer reads bearer token from Authorization header.
        const mcpAuthorizer = new apigw.TokenAuthorizer(this, 'McpAuthorizer', {
            handler: mcpAuthorizerFn,
            identitySource: apigw.IdentitySource.header('Authorization')
        });

        // Route all HTTP verbs to the same MCP Lambda handler.
        // MCP transport inside the app handles method-level semantics.
        mcpResource.addMethod('ANY', new apigw.LambdaIntegration(bookingsMcpServerFn), {
            authorizer: mcpAuthorizer,
            authorizationType: apigw.AuthorizationType.CUSTOM
        });

        // Endpoint exported for other constructs (e.g., AgentConstruct) and post-deploy scripts.
        const mcpEndpoint = `${mcpApi.url}mcp`;

        new CfnOutput(this, 'McpEndpoint', {
            value: mcpEndpoint
        })

        return { mcpEndpoint };
    }
}

module.exports = McpServerConstruct;