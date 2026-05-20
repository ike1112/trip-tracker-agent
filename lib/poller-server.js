const iam     = require('aws-cdk-lib/aws-iam');
const lambda  = require('aws-cdk-lib/aws-lambda');
const events  = require('aws-cdk-lib/aws-events');
const targets = require('aws-cdk-lib/aws-events-targets');
const { Duration, CfnOutput, Stack } = require('aws-cdk-lib');
const { Construct } = require('constructs');

// Default Bedrock model for the alert-decision call. Override at deploy:
//   cdk deploy -c bedrockModelId=...
const DEFAULT_BEDROCK_MODEL_ID = 'claude-haiku-4-5-20251001';

/**
 * PollerServerConstruct — provisions the trip-tracker poller Lambda + its
 * EventBridge schedule.
 *
 * Design notes:
 *
 * - Python 3.13 ARM64, matching the travel-agent Lambda. The poller does not
 *   need the Strands/Pydantic-heavy dependency layer the agent uses, so it
 *   ships its own lightweight requirements.txt (boto3 + powertools + pyjwt
 *   + xray-sdk). Cold-start stays small.
 *
 * - X-Ray ACTIVE per production-readiness companion §3.3 — the trace tells
 *   the poll-path story (EventBridge → Poller → MCPs → DDB) end-to-end.
 *
 * - Reserved concurrency = 1. Prevents an EventBridge tick from launching
 *   a second Lambda while the previous one is still walking watches. At
 *   4h cadence this is rare; it's free at AWS and the right default.
 *
 * - IAM scope: read-only on Watches, read+write on FareHistory (the
 *   poller writes snapshots and the anomaly gate reads the 30-day
 *   window), and `bedrock:InvokeModel` resource-scoped to the specific
 *   foundation-model ARN. Never `bedrock:*` and never `Resource: '*'`.
 *
 * - The EventBridge rule fires the poller on a cron. Cadence comes from
 *   `pollIntervalMinutes` context with a 240-minute default (clamped to
 *   [15, 1440] so a misconfig can't turn this into a per-minute polling
 *   storm). Override at deploy:
 *       cdk deploy -c pollIntervalMinutes=15
 */
class PollerServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // Cadence resolved from CDK context (`-c pollIntervalMinutes=15`),
        // defaulting to 240 (= 4h, design-spec §5). Clamped [15, 1440] so a
        // misconfigured deploy can't run the poller every minute (rate-limit
        // / Bedrock-cost runaway). The threat model's `[5]` cost-runaway
        // row documents this defence.
        const pollIntervalMinutes = Math.max(15, Math.min(1440,
            scope.node.tryGetContext('pollIntervalMinutes') ?? 240));
        // Lambda timeout scales with watch count: each watch costs at most
        // 2 × MCP_TIMEOUT_SECONDS (15s) sequentially, plus headroom for DDB
        // writes and snapshot composition. Override with -c lambdaTimeoutSeconds=N
        // when watches climb above ~2. Default 60 covers personal-scale
        // (1–2 active watches at once). Clamped [30, 300] — 5 minutes is
        // the cost ceiling once Bedrock is wired into the per-watch path.
        const lambdaTimeoutSeconds = Math.max(30, Math.min(300,
            scope.node.tryGetContext('lambdaTimeoutSeconds') ?? 60));

        // Bedrock decision call config. `bedrockModelId` context lets a
        // deploy pin a specific model (e.g., a newer Haiku) without a
        // code change. `bedrockMode` toggles live vs stub — `live` is the
        // production default; deploy with `-c bedrockMode=stub` for a
        // cost-free staging stack.
        const bedrockModelId = scope.node.tryGetContext('bedrockModelId') ?? DEFAULT_BEDROCK_MODEL_ID;
        const bedrockMode    = scope.node.tryGetContext('bedrockMode')    ?? 'live';
        // Catch typos at synth time rather than waiting for a Lambda
        // cold-start ImportError to surface in CloudWatch.
        const ALLOWED_BEDROCK_MODES = ['live', 'stub'];
        if (!ALLOWED_BEDROCK_MODES.includes(bedrockMode)) {
            throw new Error(
                `bedrockMode context value must be one of ${ALLOWED_BEDROCK_MODES.join(', ')}; got: ${bedrockMode}`
            );
        }
        // Blank/whitespace bedrockModelId would synthesise a broken IAM
        // ARN that CloudFormation accepts but every InvokeModel call
        // denies — fail at synth time instead.
        if (!bedrockModelId || String(bedrockModelId).trim() === '') {
            throw new Error('bedrockModelId context value must not be blank');
        }

        const pollerFn = new lambda.Function(this, 'PollerFn', {
            functionName: 'trip-tracker-poller',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: 'app.handler',
            timeout: Duration.seconds(lambdaTimeoutSeconds),
            memorySize: 512,
            tracing: lambda.Tracing.ACTIVE,
            // Prevent overlapping invocations if a poll runs longer than the
            // EventBridge interval.
            reservedConcurrentExecutions: 1,
            code: lambda.Code.fromAsset('./lambdas/poller', {
                exclude: [
                    '.venv/**', '.venv', '.venv-tests/**',
                    '*.pyc', '__pycache__/**', '.idea/**',
                    'tests/**', 'dev-requirements.txt',
                ],
            }),
            // Shared dependencies layer (powertools + xray-sdk + pyjwt)
            // — the user-code zip above ships ONLY source, no pip deps.
            // boto3 is in the Lambda runtime; the rest come from the layer
            // built once in AgentConstruct and shared across all three
            // Python Lambdas to keep layer versions consistent.
            layers: [props.dependenciesLayer],
            environment: {
                WATCHES_TABLE_NAME:      props.watchesTable.tableName,
                FARE_HISTORY_TABLE_NAME: props.fareHistoryTable.tableName,
                // Per-watch MCP calls. The poller mints JWTs with
                // sub=trip-tracker-poller signed by its OWN secret (ADR
                // 0006). The mcp-authorizer Lambda and the MCP server
                // handlers both re-verify, coupling each secret to its
                // allowed sub (see threat-model boundary [2]).
                POLLER_JWT_SECRET_ARN:  props.pollerJwtSecret.secretArn,
                FLIGHTS_MCP_ENDPOINT:   props.flightsMcpEndpoint,
                HOTELS_MCP_ENDPOINT:    props.hotelsMcpEndpoint,
                // Bedrock Haiku 4.5 alert-decision call (ADR 0004).
                BEDROCK_MODEL_ID: bedrockModelId,
                BEDROCK_MODE:     bedrockMode,
                // Alert-notifier function name — the poller async-invokes
                // it when decision.alert is true (ADR 0005). Empty means
                // notifier wiring is intentionally absent (older stack,
                // manual debugging) and the poller logs a warning and
                // skips.
                NOTIFIER_FUNCTION_NAME: props.notifierFunction ? props.notifierFunction.functionName : '',
                // Powertools log-level / service-name / metrics-namespace.
                POWERTOOLS_SERVICE_NAME: 'trip-tracker-poller',
                LOG_LEVEL: 'INFO',
            },
        });

        // DDB grants: read-only on Watches (enumeration), read+write on
        // FareHistory (snapshot writes plus the anomaly gate's 30-day
        // window query).
        props.watchesTable.grantReadData(pollerFn);
        props.fareHistoryTable.grantReadWriteData(pollerFn);

        // Least-privilege read on the poller's OWN signing secret only
        // (ADR 0006). grantRead scopes secretsmanager:GetSecretValue to
        // the ARN; the poller can never read the agent's secret.
        props.pollerJwtSecret.grantRead(pollerFn);

        // Bedrock grant: `bedrock:InvokeModel` resource-scoped to the
        // chosen foundation-model ARN. NOT `Resource: '*'` and NOT
        // `bedrock:*` — the poller never needs to enumerate models or
        // invoke anything else. If a deploy ever switches to a cross-
        // region inference profile, supply its full ARN explicitly via
        // the `bedrockInferenceProfileArn` context (the format is
        // account-scoped and uses `inference-profile/...` resource
        // type, which we can't synthesise from the model id alone).
        const region = Stack.of(this).region;
        const bedrockResources = [`arn:aws:bedrock:${region}::foundation-model/${bedrockModelId}`];
        const inferenceProfileArn = scope.node.tryGetContext('bedrockInferenceProfileArn');
        if (inferenceProfileArn) {
            // Reject obviously-wrong overrides at synth time so a typo
            // (e.g. `"*"` or `"arn:aws:bedrock:*:*:*"`) cannot silently
            // widen the IAM grant. The threat-model `[6]` row claims
            // this validation lives here — make it true.
            const inferenceProfilePattern = /^arn:aws:bedrock:[a-z0-9-]+:\d{12}:inference-profile\/[\w.-]+$/;
            if (!inferenceProfilePattern.test(inferenceProfileArn)) {
                throw new Error(
                    `bedrockInferenceProfileArn must match arn:aws:bedrock:<region>:<account>:inference-profile/<id>; got: ${inferenceProfileArn}`
                );
            }
            bedrockResources.push(inferenceProfileArn);
        }
        pollerFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['bedrock:InvokeModel'],
            resources: bedrockResources,
        }));

        // Notifier invoke grant — async fire-and-forget (the runtime
        // event-invoke path requires `lambda:InvokeFunction`).
        // Resource-scoped to the notifier ARN, never `Resource: '*'`.
        // Grant is conditional on a notifier function being wired in
        // so local / older-stack synth still works.
        if (props.notifierFunction) {
            props.notifierFunction.grantInvoke(pollerFn);
        }

        // EventBridge schedule. Cadence comes from the `pollIntervalMinutes`
        // CDK context (default 240 = 4h, design-spec §5). Override at
        // deploy time:  cdk deploy -c pollIntervalMinutes=15
        const rule = new events.Rule(this, 'PollerSchedule', {
            schedule: events.Schedule.rate(Duration.minutes(pollIntervalMinutes)),
            enabled: true,
            description: `Trip-tracker poller cron (every ${pollIntervalMinutes} minutes).`,
        });
        rule.addTarget(new targets.LambdaFunction(pollerFn));

        // Surface the function name + cadence so post-deploy scripts and
        // dashboards can find them without hard-coding.
        new CfnOutput(this, 'PollerFunctionName', { value: pollerFn.functionName });
        new CfnOutput(this, 'PollerCadenceMinutes', { value: String(pollIntervalMinutes) });
        new CfnOutput(this, 'PollerBedrockModelId', { value: bedrockModelId });
        new CfnOutput(this, 'PollerBedrockMode',    { value: bedrockMode });

        // Expose so other constructs can attach IAM grants or env vars.
        // Uniform `this.function` across all five primary constructs lets
        // the observability dashboard wire Lambda widgets the same way
        // regardless of which construct supplied the ref.
        this.function = pollerFn;
        this.scheduleRule = rule;
    }
}

module.exports = PollerServerConstruct;
