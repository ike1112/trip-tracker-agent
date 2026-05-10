// CDK construct libraries for the AWS services this construct provisions
const iam    = require('aws-cdk-lib/aws-iam');       // IAM roles and policies
const lambda = require('aws-cdk-lib/aws-lambda');    // Lambda functions and layers
const apigw  = require('aws-cdk-lib/aws-apigateway'); // REST API + authorizer
const ddb    = require('aws-cdk-lib/aws-dynamodb');   // (imported for potential future use)
const s3     = require('aws-cdk-lib/aws-s3');         // S3 bucket for session state
const { Duration, RemovalPolicy, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

/**
 * AgentConstruct provisions every AWS resource needed to run the Strands
 * travel agent behind a secure, authenticated REST API:
 *
 *   S3 bucket          – stores per-user conversation session state so the
 *                        Lambda function can remain stateless between invocations
 *   Lambda layer       – pre-built Python dependencies (Strands SDK, boto3, etc.)
 *                        packaged separately to keep the function zip small and
 *                        to allow re-use across multiple functions
 *   Travel-agent fn    – the core AI agent that processes user messages by
 *                        chaining LLM calls (via Bedrock) and MCP tool calls
 *   API Gateway        – exposes the agent function as a POST endpoint that the
 *                        web UI can call over HTTPS
 *   Authorizer fn      – a token-based Lambda authorizer that validates the
 *                        Cognito JWT on every inbound request, blocking
 *                        unauthenticated callers before they reach the agent
 */
class AgentConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // -----------------------------------------------------------------------
        // Session store bucket
        // The travel-agent Lambda is stateless, but a multi-turn conversation
        // requires memory of prior messages.  This bucket stores per-user session
        // objects (keyed by username) so conversation history survives Lambda
        // cold-starts and concurrent invocations.
        // DESTROY + autoDeleteObjects makes teardown clean in dev/test environments.
        // -----------------------------------------------------------------------
        const agentSessionStoreBucket = new s3.Bucket(this, 'AgentSessionStore', {
            removalPolicy: RemovalPolicy.DESTROY,
            autoDeleteObjects: true
        });

        // -----------------------------------------------------------------------
        // Dependencies Lambda layer
        // Why a layer?  The Python packages required by the Strands agent
        // (LLM SDK, HTTP clients, etc.) are large.  Packaging them as a shared
        // layer means:
        //   - the function code zip stays small → faster deploys & cold-starts
        //   - the layer can be reused by other functions in the stack
        //
        // The bundling block cross-compiles for manylinux2014_aarch64 so the
        // native binaries match the ARM64 Lambda execution environment.
        // -----------------------------------------------------------------------
        const dependenciesLayer = new lambda.LayerVersion(this, 'DependenciesLayer', {
            removalPolicy: RemovalPolicy.DESTROY,
            compatibleArchitectures: [props.fnArchitecture],
            code: lambda.Code.fromAsset('./layers/dependencies', {
                bundling: {
                    image: lambda.Runtime.PYTHON_3_13.bundlingImage,
                    platform: 'linux/arm64',
                    command: [
                        'bash',
                        '-c',
                        // Install wheels that match the Lambda runtime (ARM64, CPython 3.13),
                        // then copy the source files alongside them in /asset-output/python
                        'pip install --no-cache-dir --platform manylinux2014_aarch64 --only-binary=:all: --implementation cp --python-version 3.13 -r requirements.txt -t /asset-output/python && cp -au . /asset-output/python'
                    ]
                }
            })
        });

        // -----------------------------------------------------------------------
        // Travel-agent Lambda function
        // This function receives a user message, manages conversation history
        // via the session store, and orchestrates calls to Amazon Bedrock (LLM)
        // and the MCP server (tools like car/hotel booking).
        //
        // Key environment variables injected at deploy time:
        //   MCP_ENDPOINT            – URL of the MCP server Lambda so the agent
        //                             can call tools without hard-coding the address
        //   JWT_SIGNATURE_SECRET    – used to sign internal JWTs passed to the MCP
        //                             server, proving requests originate from this agent
        //   SESSION_STORE_BUCKET_NAME – where to read/write conversation history
        //   COGNITO_JWKS_URL        – used to verify the user's token inside the agent
        // -----------------------------------------------------------------------
        const travelAgentFn = new lambda.Function(this, 'TravelAgent', {
            functionName: 'travel-agent-on-lambda',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: 'app.handler',
            timeout: Duration.seconds(30),    // generous timeout to cover LLM + tool round-trips
            memorySize: 1024,                 // more memory also increases vCPU allocation → faster inference
            tracing: lambda.Tracing.ACTIVE,   // slice 3: X-Ray on every Lambda so the full request path traces end-to-end
            code: lambda.Code.fromAsset('./lambdas/travel-agent', {
                exclude: ['.venv/**', '.venv', '.venv-tests/**', '*.pyc', '__pycache__/**', '.idea/**', 'tests/**', 'dev-requirements.txt', '']
            }),
            layers: [dependenciesLayer],
            environment: {
                MCP_ENDPOINT: props.mcpEndpoint,
                // MCP endpoints (slices 3 + 4). mcp_client_manager.py iterates
                // these in order, merges their tool lists, and tolerates any
                // one being down so a single MCP outage doesn't take the agent
                // off-line.
                FLIGHTS_MCP_ENDPOINT: props.flightsMcpEndpoint,
                HOTELS_MCP_ENDPOINT: props.hotelsMcpEndpoint,
                JWT_SIGNATURE_SECRET: props.jwtSignatureSecret,
                SESSION_STORE_BUCKET_NAME: agentSessionStoreBucket.bucketName,
                COGNITO_JWKS_URL: props.cognitoJwksUrl,
                // Trip-tracker tables (slice 1). The names are CFN refs, so the
                // running Lambda always points at the tables this stack created
                // — no risk of drift between deploys or environments.
                WATCHES_TABLE_NAME:      props.watchesTable.tableName,
                FARE_HISTORY_TABLE_NAME: props.fareHistoryTable.tableName,
            }
        });

        // Grant the agent permission to call Bedrock foundation models.
        // Resources: '*' is required here because Bedrock model ARNs are not
        // known until runtime (the model ID is chosen in agent config).
        travelAgentFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
            resources: ['*'],
        }));

        // Allow the agent to read and write its session objects in S3.
        // grantReadWrite creates the least-privilege S3 policy automatically.
        agentSessionStoreBucket.grantReadWrite(travelAgentFn);

        // Trip-tracker DDB grants. Watch CRUD tools (slice 2) need both reads
        // and writes on Watches; the agent only reads FareHistory (the poller
        // in slice 5 will be the writer there). grantReadWriteData /
        // grantReadData emit the minimum-necessary action set for us.
        props.watchesTable.grantReadWriteData(travelAgentFn);
        props.fareHistoryTable.grantReadData(travelAgentFn);

        // -----------------------------------------------------------------------
        // API Gateway REST API
        // Exposes the agent as a public HTTPS POST endpoint.  REGIONAL deployment
        // means the API is served from the same AWS region as the Lambda, avoiding
        // the extra latency of a CloudFront-fronted EDGE endpoint.
        // -----------------------------------------------------------------------
        const agentApi = new apigw.RestApi(this, 'AgentApi', {
            restApiName: 'travel-agent-api',
            endpointTypes: [apigw.EndpointType.REGIONAL],
            deploy: true
        });

        // -----------------------------------------------------------------------
        // Authorizer Lambda function
        // Problem: API Gateway needs to authenticate callers before forwarding
        // requests to the agent, but the agent itself should not handle auth.
        //
        // Solution: a TOKEN authorizer — API Gateway extracts the Bearer token
        // from the Authorization header and invokes this function first.  The
        // function validates the JWT against Cognito's public keys (JWKS).  Only
        // if the token is valid does API Gateway forward the request to the agent.
        // Invalid or missing tokens are rejected with a 401/403 before the agent
        // Lambda is ever invoked, reducing cost and attack surface.
        // -----------------------------------------------------------------------
        const agentAuthorizerFn = new lambda.Function(this, 'AgentAuthorizerFn', {
            functionName: 'travel-agent-authorizer',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.NODEJS_22_X,
            handler: 'index.handler',
            timeout: Duration.seconds(10),
            memorySize: 1024,
            tracing: lambda.Tracing.ACTIVE,
            code: lambda.Code.fromAsset('./lambdas/agent-authorizer'),
            environment: {
                COGNITO_JWKS_URL: props.cognitoJwksUrl  // public endpoint — no secret required
            }
        });

        // Wire the authorizer function to API Gateway; it reads the token from
        // the Authorization header on every inbound request.
        const agentAuthorizer = new apigw.TokenAuthorizer(this, 'AgentAuthorizer', {
            handler: agentAuthorizerFn,
            identitySource: apigw.IdentitySource.header('Authorization')
        });

        // Register POST / as the single endpoint.  The authorizer runs first;
        // the agent Lambda only receives requests that have passed JWT validation.
        agentApi.root.addMethod('POST', new apigw.LambdaIntegration(travelAgentFn), {
            authorizer: agentAuthorizer,
            authorizationType: apigw.AuthorizationType.CUSTOM
        });

        // Emit the API URL as a CloudFormation stack output so it can be read
        // after deployment and injected into the web app as AGENT_ENDPOINT_URL.
        new CfnOutput(this, 'AgentEndpointUrl', {
            value: agentApi.url
        });
    }
}

module.exports = AgentConstruct;