# Security & Compliance Audit

Source tools:
- **cfn-lint** (`mcp__aws-iac__validate_cloudformation_template`) → schema/syntax validation
- **cfn-guard** (`mcp__aws-iac__check_cloudformation_template_compliance`) → AWS security rules
- Manual review of `cdk.out/StrandsAgentOnLambdaStack.template.json` and source files

---

## TL;DR

| Area | Status |
|---|---|
| Template syntax (cfn-lint) | ✅ Valid — 0 errors, 0 warnings, 0 info |
| AWS security rules (cfn-guard) | ❌ 12 violations (mostly S3 hardening + 1 IAM wildcard) |
| Secrets handling | ⚠️ Shared JWT secret hardcoded in CDK and embedded in Lambda env vars |
| Logging / observability | ⚠️ No API Gateway access logs, no X-Ray tracing, no Lambda log retention |
| Cognito hardening | ⚠️ MFA disabled, USER_PASSWORD_AUTH enabled, HTTP localhost callback |

The project is a **functional demo** that passes lint but fails ~12 production-grade security controls. Most are quick fixes once you know they apply.

---

## 1. cfn-lint findings

```
"is_valid": true
"error_count": 0, "warning_count": 0, "info_count": 0
```

The synthesized template is structurally valid CloudFormation.

## 2. cfn-guard findings (12 violations)

cfn-guard ran the AWS-managed security rule set against the template. Violations group into two clusters:

### Cluster A — S3 bucket hardening (11 of 12)

The `AgentSessionStore` S3 bucket (Strands session state) is created with `removalPolicy: DESTROY` and `autoDeleteObjects: true` and **no other security properties**:

| Rule ID | What it wants |
|---|---|
| `S3_BUCKET_SERVER_SIDE_ENCRYPTION_ENABLED` | `BucketEncryption.ServerSideEncryptionConfiguration` with `AES256` or `aws:kms` |
| `S3_DEFAULT_ENCRYPTION_KMS` | Same — KMS preferred |
| `S3_BUCKET_LEVEL_PUBLIC_ACCESS_PROHIBITED` | `PublicAccessBlockConfiguration` with all 4 fields = `true` |
| `S3_BUCKET_PUBLIC_READ_PROHIBITED` | (same — covered by Public Access Block) |
| `S3_BUCKET_PUBLIC_WRITE_PROHIBITED` | (same) |
| `S3_BUCKET_NO_PUBLIC_RW_ACL` | (same) |
| `S3_BUCKET_VERSIONING_ENABLED` | `VersioningConfiguration.Status: Enabled` |
| `S3_BUCKET_REPLICATION_ENABLED` | Cross-region replication |
| `S3_BUCKET_LOGGING_ENABLED` | Server-access logging to a separate logging bucket |
| `S3_BUCKET_DEFAULT_LOCK_ENABLED` | Object Lock — only valuable for WORM/compliance use |
| `S3_BUCKET_SSL_REQUESTS_ONLY` | Bucket policy denying non-TLS requests |

**Why so many findings?** Recent CDK versions enable Public Access Block by default via the `@aws-cdk/aws-s3:publicAccessBlockedByDefault: true` feature flag (set in `cdk.json` at line 92). However, cfn-guard checks for **explicit** properties on the resource. Newer L2 properties may also satisfy them — but the synthesized template is what cfn-guard sees.

**Recommended fix for the demo** — change `lib/agent.js:13-16` from:

```javascript
const agentSessionStoreBucket = new s3.Bucket(this, 'AgentSessionStore', {
    removalPolicy: RemovalPolicy.DESTROY,
    autoDeleteObjects: true
});
```

to:

```javascript
const agentSessionStoreBucket = new s3.Bucket(this, 'AgentSessionStore', {
    removalPolicy: RemovalPolicy.DESTROY,
    autoDeleteObjects: true,
    encryption: s3.BucketEncryption.S3_MANAGED,         // SSE-S3 (AES256)
    blockPublicAccess: s3.BlockPublicAccess.BLOCK_ALL,  // all 4 fields = true
    enforceSSL: true,                                    // SSL-only bucket policy
    versioned: true                                      // versioning
});
```

That fixes 7 of the 11 S3 violations with idiomatic L2 props. **Replication, server-access logging, and Object Lock** are not appropriate for a per-user session store and should be left disabled — but document the choice.

### Cluster B — IAM wildcard (1 of 12)

| Rule ID | What it wants |
|---|---|
| `IAM_POLICYDOCUMENT_NO_WILDCARD_RESOURCE` | Don't grant `Resource: "*"` |

`lib/agent.js:52-55`:

```javascript
travelAgentFn.addToRolePolicy(new iam.PolicyStatement({
    actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream'],
    resources: ['*'],   // ← finding
}));
```

This is the agent's permission to call Bedrock. The `*` is **partially defensible**: Bedrock cross-region inference profiles resolve to model ARNs across multiple regions at call time, so scoping to a single ARN can break inference. Still, the right fix is to scope to:

```javascript
resources: [
  `arn:aws:bedrock:*::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0`,
  // for cross-region inference profiles, also include the inference-profile ARN:
  `arn:aws:bedrock:*:*:inference-profile/us.anthropic.claude-3-5-haiku-*`,
]
```

This still allows model swapping but doesn't grant `bedrock:InvokeModel` on every model in the catalog.

---

## 3. Manual findings (cfn-guard didn't catch)

These required reading the CDK source and template — cfn-guard's default ruleset doesn't cover them.

### 3.1 Hardcoded shared JWT secret

`lib/strands-agent-on-lambda-stack.js:10`:

```javascript
const JWT_SIGNATURE_SECRET = 'jwt-signature-secret';
```

Synthesized into Lambda env vars (visible in template, CFN console, `aws lambda get-function-configuration`):

```json
"Environment": { "Variables": { "JWT_SIGNATURE_SECRET": "jwt-signature-secret" } }
```

**Severity: high.** The secret signs the agent → MCP token. Anyone who reads CloudFormation can mint tokens and call the MCP server with any `user_id` they want.

**Fix:** put it in Secrets Manager and let both Lambdas read it at cold-start:

```javascript
const jwtSecret = new secretsmanager.Secret(this, 'McpJwtSecret', {
    generateSecretString: { passwordLength: 64, excludePunctuation: true }
});
// in agent + mcp authorizer + mcp server:
fn.addEnvironment('JWT_SECRET_ARN', jwtSecret.secretArn);
jwtSecret.grantRead(fn);
```

The README itself acknowledges this on the Cognito client secret output (`lib/cognito.js:90-91`) but didn't apply the fix to the JWT secret.

### 3.2 Cognito User Pool Client allows password auth

`lib/cognito.js:21-23` (and template):

```json
"ExplicitAuthFlows": ["ALLOW_USER_PASSWORD_AUTH", "ALLOW_REFRESH_TOKEN_AUTH"]
```

`ALLOW_USER_PASSWORD_AUTH` lets a client call `InitiateAuth` with username + password directly, **bypassing the hosted UI**. The web app uses the hosted UI flow, so this auth flow isn't needed and is a wider attack surface than necessary.

**Fix:** drop `userPassword: true` from `authFlows` in `lib/cognito.js:21-23`, keep only the OAuth code grant flow.

### 3.3 Cognito MFA, advanced security off

The user pool has no MFA and is on the Lite tier (no advanced security / threat detection). For a demo this is fine; for production with real users, enable Plus tier features (compromised-credentials check, adaptive auth) and require MFA for sign-in.

### 3.4 Callback URL is HTTP localhost

`lib/cognito.js:5`:
```javascript
const REDIRECT_URI = "http://localhost:8000/callback";
```

Cognito allows HTTP only for `localhost`; in production you need an HTTPS endpoint. Source: AWS docs — *"Amazon Cognito requires HTTPS over HTTP except for localhost addresses (used for testing)"* (AWS SDK docs for `callbackUrls`).

### 3.5 No Lambda log retention

None of the four Lambda functions sets `logRetention`. CDK creates the log groups implicitly with **infinite retention** (the AWS default). This is both a cost leak and a compliance issue (logs may contain JWTs or PII forever).

**Fix:** add `logRetention: logs.RetentionDays.ONE_WEEK` (or similar) to every `lambda.Function` in `lib/agent.js` and `lib/mcp-server.js`.

### 3.6 No API Gateway access logging or X-Ray

Both REST APIs are created with `deploy: true` and otherwise default options. There's no `accessLogDestination`, no `accessLogFormat`, no `tracingEnabled`. Result:
- Execution logs are off (need Stage `MethodSettings.LoggingLevel`)
- Access logs are off (need a CloudWatch log group + format string)
- Distributed tracing is off

For a demo this is acceptable — for production you can't troubleshoot anything.

Reference: [Set up CloudWatch logging for REST APIs](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html).

### 3.7 Web session cookie uses `secret_key="secret"`

`web/app.py:18`:
```python
fastapi_app.add_middleware(SessionMiddleware, secret_key="secret")
```

Starlette signs (HMAC) the session cookie with this key. With `"secret"` hardcoded, anyone can forge a session cookie and impersonate any logged-in user. The cookie is integrity-protected, not encrypted.

**Fix:** generate a random key and source it from environment:

```python
secret_key=os.environ["SESSION_SECRET_KEY"]
```

### 3.8 Authorizer cache TTL = 5 minutes (default)

Both `apigw.TokenAuthorizer` constructions don't set `resultsCacheTtl`, so they use the default `Duration.minutes(5)` per the [CDK docs](https://docs.aws.amazon.com/cdk/api/v1/python/aws_cdk.aws_apigateway/RequestAuthorizerProps.html). After a user is logged out, their JWT remains "valid" via cache for up to 5 min. You can keep this trade-off but at least `resultsCacheTtl: Duration.seconds(60)` would shrink the window 5×.

---

## 4. Severity-ordered remediation list

1. **CRITICAL — hardcoded JWT secret** (§3.1). Anyone with template/CloudFormation read access can mint MCP tokens.
2. **HIGH — web session secret = "secret"** (§3.7). Trivial cookie forgery.
3. **HIGH — Cognito `ALLOW_USER_PASSWORD_AUTH`** (§3.2). Unused, broadens attack surface.
4. **MEDIUM — S3 bucket missing encryption / public-access block / SSL-only / versioning** (§2 cluster A). Four-line CDK fix.
5. **MEDIUM — IAM `Resource: "*"` on Bedrock** (§2 cluster B). Acceptable for demo, scope down for prod.
6. **MEDIUM — no Lambda log retention** (§3.5). Cost + compliance leak.
7. **LOW — no API Gateway logging / X-Ray** (§3.6). Demo is fine; prod is blind.
8. **LOW — Cognito MFA off, no Plus tier** (§3.3). Per-environment policy call.
9. **INFO — authorizer cache 5 min** (§3.8). Document the trade-off.

---

## 5. What the project does *right* (security-wise)

To be fair:

- ✅ **Token exchange at trust boundary** — agent does not forward user JWT to MCP, it mints a new one with `sub=travel-agent`. This is the correct pattern (see `analysis/identity-flow.md`).
- ✅ **Two layers of JWT validation on MCP** — API Gateway authorizer + Express middleware. Defense in depth.
- ✅ **Hosted UI flow** — password never touches the web app.
- ✅ **`AdminCreateUserConfig.AllowAdminCreateUserOnly: true`** — no self-signup.
- ✅ **`AccessTokenValidity: 480` (8 hours)** — bounded session lifetime, not 30 days.
- ✅ **`autoDeleteObjects: true` + `removalPolicy: DESTROY`** — clean teardown for a demo.
- ✅ **arm64 Lambdas** — slightly cheaper *and* slightly less attack surface vs x86.
- ✅ **No public S3 bucket policy** — the `AgentSessionStorePolicy` only grants the auto-delete provider; bucket isn't world-readable despite missing the explicit Public Access Block.

---

## 6. References (AWS docs)

- [API Gateway Lambda authorizer cache TTL](https://docs.aws.amazon.com/cdk/api/v1/python/aws_cdk.aws_apigateway/RequestAuthorizerProps.html) (default 300s, max 3600s)
- [Cognito callback URL HTTPS requirement](https://docs.aws.amazon.com/sdk-for-kotlin/api/latest/cognitoidentityprovider/aws.sdk.kotlin.services.cognitoidentityprovider.model/-user-pool-client-type/callback-urls.html)
- [API Gateway CloudWatch logging setup](https://docs.aws.amazon.com/apigateway/latest/developerguide/set-up-logging.html)
- [Cognito Hosted UI](https://docs.aws.amazon.com/help-panel/cognito/latest/console/hp-hosted-ui.html)
- CDK best practices on secrets: prefer Secrets Manager over env vars (`mcp__aws-iac__cdk_best_practices` "Secrets and Sensitive Data")
