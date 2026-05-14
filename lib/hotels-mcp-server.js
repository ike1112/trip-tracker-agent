const lambda = require('aws-cdk-lib/aws-lambda');
const apigw  = require('aws-cdk-lib/aws-apigateway');
const { Duration, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

/**
 * HotelsMcpServerConstruct — provisions the hotels MCP Lambda
 * (LiteAPI-backed) + its API Gateway + JWT authorizer.
 *
 * Mirrors FlightsMcpServerConstruct:
 *   - Direct Lambda handler, no Express, no LWA. See ADR 0002.
 *   - X-Ray tracing ACTIVE on both the server Lambda and the authorizer.
 *   - Fixture replay mode default so reviewers can deploy without
 *     a LiteAPI key.
 *   - Exposes `this.endpoint / this.function / this.api /
 *     this.authorizerFunction`. No explicit constructor return.
 *
 * Least privilege: this construct only attaches CloudWatch + X-Ray.
 * It has zero DDB / S3 / Bedrock access — its only job is outbound HTTPS
 * to LiteAPI. Blast radius of a compromise stops at the Lambda's own
 * env vars (which contain the LiteAPI key but nothing else load-bearing).
 */
class HotelsMcpServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        const hotelsMcpFn = new lambda.Function(this, 'HotelsMcpServer', {
            functionName: 'hotels-mcp-server',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'index.handler',
            // Hotel searches are slower than flight searches (multi-property,
            // multi-rate-plan). The live client times out at 20s; this Lambda
            // budget allows 10s on top for serialisation + X-Ray flush.
            timeout: Duration.seconds(30),
            memorySize: 512,
            tracing: lambda.Tracing.ACTIVE,
            code: lambda.Code.fromAsset('./lambdas/hotels-mcp', {
                exclude: ['tests/**', '.nyc_output/**', '*.log', '.git/**']
            }),
            environment: {
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret,
                MCP_MODE: props.mcpMode ?? 'fixture',
                LITEAPI_API_KEY: props.liteApiKey ?? '',
            }
        });

        // Authorizer Lambda — same source asset as the flights authorizer
        // (lambdas/mcp-authorizer/index.js). Same JWT secret, same `sub`
        // requirement. Could be shared across both MCP APIs, but keeping
        // per-API instances means each endpoint has its own deny-by-default
        // surface and its own CloudWatch metric source for the dashboard.
        const hotelsAuthorizerFn = new lambda.Function(this, 'HotelsMcpAuthorizerFn', {
            functionName: 'hotels-mcp-server-authorizer',
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

        const hotelsApi = new apigw.RestApi(this, 'HotelsMcpApi', {
            restApiName: 'hotels-mcp-api',
            endpointTypes: [apigw.EndpointType.REGIONAL],
            deploy: true,
        });

        const mcpResource = hotelsApi.root.addResource('mcp');

        const authorizer = new apigw.TokenAuthorizer(this, 'HotelsMcpAuthorizer', {
            handler: hotelsAuthorizerFn,
            identitySource: apigw.IdentitySource.header('Authorization'),
        });

        mcpResource.addMethod('ANY', new apigw.LambdaIntegration(hotelsMcpFn), {
            authorizer,
            authorizationType: apigw.AuthorizationType.CUSTOM,
        });

        const hotelsMcpEndpoint = `${hotelsApi.url}mcp`;

        new CfnOutput(this, 'HotelsMcpEndpoint', { value: hotelsMcpEndpoint });
        new CfnOutput(this, 'HotelsMcpMode',     { value: props.mcpMode ?? 'fixture' });

        this.endpoint           = hotelsMcpEndpoint;
        this.function           = hotelsMcpFn;
        this.api                = hotelsApi;
        this.authorizerFunction = hotelsAuthorizerFn;
    }
}

module.exports = HotelsMcpServerConstruct;
