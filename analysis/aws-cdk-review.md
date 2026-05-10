# CDK Best-Practices Review

Source tools:
- **AWS IaC MCP server** `cdk_best_practices` (official guide)
- Manual review of `lib/*.js`, `bin/strands-agent-on-lambda.js`, `cdk.json`, `package.json`

---

## TL;DR

This is a **good demo-quality CDK project**. It uses L2 constructs, generated names mostly, and `grant*` helpers — all the right reflexes. It misses about a dozen production hygiene practices around statefulness boundaries, tests, log retention, observability, and config-via-properties.

---

## 1. What's done right

| Best practice | Where |
|---|---|
| App entry in `bin/`, stack in `lib/` | `bin/strands-agent-on-lambda.js`, `lib/strands-agent-on-lambda-stack.js` |
| L2 constructs throughout (no L1 escape hatches) | All of `lib/agent.js`, `lib/cognito.js`, `lib/mcp-server.js` |
| `grant*` helper for IAM | `agentSessionStoreBucket.grantReadWrite(travelAgentFn)` (`lib/agent.js:57`) |
| Construct decomposition (Cognito / Mcp / Agent) | `lib/strands-agent-on-lambda-stack.js:16-32` — three sub-constructs, not one mega-stack |
| `removalPolicy` set explicitly | S3 bucket (`lib/agent.js:14`), DependenciesLayer (`lib/agent.js:19`) |
| `RemovalPolicy.DESTROY` + `autoDeleteObjects: true` for dev | `lib/agent.js:14-15` — clean teardown |
| No CloudFormation parameters / conditions | Decisions made in JS; `FN_ARCHITECTURE` is a JS const |
| Generated names where appropriate | UserPool, UserPoolClient, etc. are unnamed |
| Modern CDK feature flags enabled | `cdk.json` has 50+ feature flags including `publicAccessBlockedByDefault: true` |

The architecture is also sensibly split by concern: each `Construct` corresponds to a logical service domain (auth / mcp / agent).

---

## 2. Gaps vs. the official CDK best-practices guide

These are tagged with the section from `cdk_best_practices` so you can read the source guidance.

### 2.1 Hardcoded physical names (Resource Naming)

`lib/agent.js`:
- `functionName: 'travel-agent-on-lambda'` (line 34)
- `functionName: 'travel-agent-authorizer'` (line 67)
- `restApiName: 'travel-agent-api'` (line 60)

`lib/mcp-server.js`:
- `functionName: 'bookings-mcp-server'` (line 14)
- `functionName: 'bookings-mcp-server-authorizer'` (line 38)
- `restApiName: 'travel-agent-mcp-api'` (line 30)

CDK guide says:
> Use generated names, not physical names. … Hardcoded names prevent multiple deployments and resource replacement.

**Concrete consequence:** trying to deploy two copies of this stack to the same account/region (e.g., dev + staging) will fail with `Function already exists`. The fix is to remove the `functionName`/`restApiName` props and let CDK generate unique names; pass the generated name via `Ref`/`getAtt` to anything that needs it.

### 2.2 Hardcoded JWT secret as a JavaScript constant (Configuration & Secrets)

`lib/strands-agent-on-lambda-stack.js:10`:
```javascript
const JWT_SIGNATURE_SECRET = 'jwt-signature-secret';
```

CDK guide says:
> Use Secrets Manager and Parameter Store. … Never hardcode credentials or secrets in code.

This violates two best practices at once: hardcoded secret AND configuration as constant. The right pattern:

```javascript
const jwtSecret = new secretsmanager.Secret(this, 'McpJwtSecret');
fn.addEnvironment('JWT_SECRET_ARN', jwtSecret.secretArn);
jwtSecret.grantRead(fn);
```

Plus a small at-cold-start hook in each Lambda to fetch and cache the secret value (use the [Secrets Manager Lambda extension](https://docs.aws.amazon.com/secretsmanager/latest/userguide/retrieving-secrets_lambda.html) so it's free + fast).

See also `analysis/aws-security.md` §3.1 — this is also security-CRITICAL.

### 2.3 No log retention (Resource Management)

CDK guide says:
> Default CDK behavior retains all data and logs forever. Explicitly set removal policies for production resources.

None of the four Lambda functions sets `logRetention`. Add to each:
```javascript
logRetention: logs.RetentionDays.ONE_WEEK,
```
(or `TWO_WEEKS`, etc.)

### 2.4 Stateless and stateful resources in the same stack (Stack Organization)

CDK guide says:
> Keep stateful resources (databases, S3 buckets) in separate stacks. Enable termination protection on stateful stacks.

This stack mixes the **stateful** `AgentSessionStore` S3 bucket and the Cognito User Pool (which holds Alice/Bob accounts) with **stateless** Lambdas + API Gateway. For a demo, fine. For production, splitting into:
- `IdentityStack` (Cognito) with `terminationProtection: true`
- `DataStack` (S3 session bucket) with `terminationProtection: true`
- `AppStack` (Lambdas, API GWs) — freely redeployable

…makes the blast radius of a faulty deploy much smaller.

### 2.5 No infrastructure unit tests (Testing)

CDK guide says:
> Write tests confirming generated templates match expectations. Test that logical IDs of stateful resources remain static.

`package.json` line 10 has `"test": "jest"` and `jest.config.js` is present, but there are **no test files**. A starter suite:

```javascript
// test/snapshot.test.js
const { Template } = require('aws-cdk-lib/assertions');
const cdk = require('aws-cdk-lib');
const { StrandsAgentOnLambdaStack } = require('../lib/strands-agent-on-lambda-stack');

test('s3 session bucket has SSE enabled', () => {
    const app = new cdk.App();
    const stack = new StrandsAgentOnLambdaStack(app, 'TestStack');
    const template = Template.fromStack(stack);
    template.hasResourceProperties('AWS::S3::Bucket', {
        BucketEncryption: { ServerSideEncryptionConfiguration: [{...}] }
    });
});

test('agent lambda logical ID is stable', () => {
    const app = new cdk.App();
    const stack = new StrandsAgentOnLambdaStack(app, 'TestStack');
    const template = Template.fromStack(stack);
    expect(Object.keys(template.findResources('AWS::Lambda::Function')))
      .toContain('AgentConstructTravelAgentB4973BBE');
});
```

### 2.6 No monitoring (Monitoring)

CDK guide says:
> Measure everything. Create metrics, alarms, and dashboards for all resources. Use L2 construct convenience methods like `metricUserErrors()`.

The stack has zero alarms or dashboards. At minimum:
```javascript
travelAgentFn.metricErrors().createAlarm(this, 'AgentErrors', { threshold: 5, evaluationPeriods: 1 });
```

Plus a Bedrock token-spend dashboard if production. Bedrock token usage is published in CloudWatch as `InputTokenCount` and `OutputTokenCount` per model.

### 2.7 No CloudWatch logs / X-Ray on API Gateway (Monitoring)

Both `apigw.RestApi` constructions use defaults. To enable production-grade observability:

```javascript
const accessLogs = new logs.LogGroup(this, 'AgentApiAccessLogs', { retention: logs.RetentionDays.ONE_WEEK });
const agentApi = new apigw.RestApi(this, 'AgentApi', {
    restApiName: 'travel-agent-api',
    deployOptions: {
        accessLogDestination: new apigw.LogGroupLogDestination(accessLogs),
        accessLogFormat: apigw.AccessLogFormat.jsonWithStandardFields(),
        loggingLevel: apigw.MethodLoggingLevel.INFO,
        tracingEnabled: true,            // X-Ray
        metricsEnabled: true,            // detailed CW metrics
    },
});
```

### 2.8 Removed `cdk-nag` consideration

CDK guide says (compliance section):
> (Optional) Use CDK Nag for compliance checks. Before applying CDK Nag compliance checks, you MUST ask the user if they would like to use CDK Nag.

Adding `cdk-nag` to this project would surface most of the cfn-guard findings at synth time and block them from ever being deployed. Worth considering.

### 2.9 Missing properties-vs-environment discipline (Configuration)

CDK guide:
> Configure with properties and methods, not environment variables. Limit environment variable lookups to the top level of the app.

The stack doesn't have many env-var lookups (good), but the JWT secret and FN_ARCHITECTURE are passed around as construct props, which is the right pattern. One small improvement: `FN_ARCHITECTURE` and `JWT_SIGNATURE_SECRET` could be stack props (constructor args) instead of file-level constants — that lets you reuse the stack class in a different `bin/` entrypoint with x86 + a different secret without editing source.

### 2.10 No `env` on the stack (`bin/strands-agent-on-lambda.js`)

`bin/strands-agent-on-lambda.js:7`:
```javascript
new StrandsAgentOnLambdaStack(app, 'StrandsAgentOnLambdaStack');
```

CDK guide:
> Specify target account and region in stack props.

Without `env`, the stack is environment-agnostic — it deploys to whichever profile/region you happen to have set, and AZ lookups are non-deterministic. Pin it:
```javascript
new StrandsAgentOnLambdaStack(app, 'StrandsAgentOnLambdaStack', {
    env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: process.env.CDK_DEFAULT_REGION ?? 'us-east-1' }
});
```

### 2.11 Outputs that don't carry export names (after our fix)

We removed `exportName` on the `CfnOutput`s during deploy debugging (collided with another stack). That's fine because no `Fn.importValue` consumes them — but it broke `prep-web.sh`, which was reading them by `ExportName`. Either:
- Re-add export names with a unique prefix (`exportName: 'StrandsAgent-CognitoJwksUrl'`)
- Update `prep-web.sh` to query by `OutputKey` (`Stacks[0].Outputs[?starts_with(OutputKey,'CognitoCognitoJwksUrl')].OutputValue`)
- Read directly from `cdk-outputs.json`

For a learning project, the third option is least magic.

---

## 3. CDK source-code observations

### 3.1 `ddb` import is unused

`lib/agent.js:4`:
```javascript
const ddb = require('aws-cdk-lib/aws-dynamodb');
```
Imported, never used. The CDK assembly probably doesn't include the `ddb` modules in the bundle (CDK is dynamic), but it's noise for a reader. Worth deleting.

### 3.2 Construct return values via `return { ... }`

`lib/cognito.js:100-102` and `lib/mcp-server.js:66`:
```javascript
return { cognitoJwksUrl }
return { mcpEndpoint };
```

Returning from a constructor is unusual JS. The idiomatic CDK pattern is exposing public properties:
```javascript
class Cognito extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);
        // ...
        this.cognitoJwksUrl = cognitoJwksUrl;
    }
}
// caller:
const cog = new Cognito(this, 'Cognito');
const url = cog.cognitoJwksUrl;
```

This pattern is what `props.mcpEndpoint` etc. expect. Returning from `super()` works in JS but Java/C#/Python ports of CDK would not allow it.

### 3.3 No JSDoc / type info on construct props

```javascript
class AgentConstruct extends Construct {
    constructor(scope, id, props) {
        // props.fnArchitecture, props.jwtSignatureSecret, props.mcpEndpoint, props.cognitoJwksUrl
```

In TypeScript this would be enforced by an interface. In JS, document with JSDoc:
```javascript
/**
 * @param {Object} props
 * @param {lambda.Architecture} props.fnArchitecture
 * @param {string} props.jwtSignatureSecret
 * @param {string} props.mcpEndpoint
 * @param {string} props.cognitoJwksUrl
 */
```

This is what the recently added comments in `lib/agent.js` (header block lines 10–26) start to do — extending it to props would be a nice next step.

### 3.4 Two stages of test invocation permissions

The synthesized template includes `ApiPermissionTest...` resources for the API Gateway test invoke endpoint. CDK's `apigateway.RestApi` adds these by default; if you don't use the test-invoke feature you can disable with `cloudWatchRole: false` and avoid the extra `Lambda::Permission` resources. Not a bug, just template bloat (4 resources).

---

## 4. Prioritized fixes

1. **Move JWT secret to Secrets Manager** (best practice + security CRITICAL).
2. **Add `logRetention` to all Lambdas** (cost + compliance).
3. **Drop hardcoded `functionName`/`restApiName`** so the stack is multi-instance friendly.
4. **Specify `env` on the stack** in `bin/strands-agent-on-lambda.js`.
5. **Add a snapshot test + 3 logical-ID stability tests** in `test/`.
6. **Enable API GW access logs + X-Ray** via `deployOptions`.
7. **Add basic CloudWatch alarms** on Lambda errors + Bedrock token spend.
8. **Split stateful resources** (Cognito + S3) into a separate stack with `terminationProtection: true` (production only).
9. **Consider `cdk-nag`** to block synth on common security issues.

---

## 5. References

- AWS IaC MCP server `cdk_best_practices` (this is the canonical source for items 1-9 above)
- [CDK Aspects + cdk-nag](https://github.com/cdklabs/cdk-nag/blob/main/RULES.md)
- [API Gateway access logging via CDK](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_apigateway.RestApi.html)
- [Lambda log retention via CDK](https://docs.aws.amazon.com/cdk/api/v2/docs/aws-cdk-lib.aws_lambda.Function.html)
