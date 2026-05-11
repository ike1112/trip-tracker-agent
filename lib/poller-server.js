const iam     = require('aws-cdk-lib/aws-iam');
const lambda  = require('aws-cdk-lib/aws-lambda');
const events  = require('aws-cdk-lib/aws-events');
const targets = require('aws-cdk-lib/aws-events-targets');
const { Duration, CfnOutput, Stack } = require('aws-cdk-lib');
const { Construct } = require('constructs');

// Slice 6 default model. Override at deploy:  cdk deploy -c bedrockModelId=...
const DEFAULT_BEDROCK_MODEL_ID = 'claude-haiku-4-5-20251001';

/**
 * PollerServerConstruct — provisions the trip-tracker poller Lambda + its
 * EventBridge schedule.
 *
 * Slice 5 task 1 (this file) lands the Lambda and the rule **disabled**.
 * Task 5 enables the rule and adds the ADR + threat-model entry.
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
 * - Reserved concurrency = 1 (locked decision in tasks/plan.md §7).
 *   Prevents an EventBridge tick from launching a second Lambda while the
 *   previous one is still walking watches. At 4h cadence this is rare;
 *   it's free at AWS and the right default.
 *
 * - IAM grant in T1 is `grantReadData` on Watches only. The FareHistory
 *   write grant is added in T3 when the snapshot writer lands; the
 *   `bedrock:InvokeModel` grant is added in slice 6 alongside the real
 *   decision call. Keeping IAM scope tight at every step.
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
        // / Bedrock-cost runaway, especially after slice 6 grants
        // `bedrock:InvokeModel`). The threat model's `[5]` cost-runaway row
        // documents this defence.
        const pollIntervalMinutes = Math.max(15, Math.min(1440,
            scope.node.tryGetContext('pollIntervalMinutes') ?? 240));
        // Lambda timeout scales with watch count: each watch costs at most
        // 2 × MCP_TIMEOUT_SECONDS (15s) sequentially, plus headroom for DDB
        // writes and snapshot composition. Override with -c lambdaTimeoutSeconds=N
        // when watches climb above ~2. Default 60 covers personal-scale
        // (1–2 active watches at once); see plan §6 risk 2. Clamped [30, 300]
        // — 5 minutes is the slice-6 cost ceiling once Bedrock is wired in.
        const lambdaTimeoutSeconds = Math.max(30, Math.min(300,
            scope.node.tryGetContext('lambdaTimeoutSeconds') ?? 60));

        // Slice 6 — Bedrock decision call. `bedrockModelId` context lets
        // a deploy pin a specific model (e.g., a newer Haiku) without a
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
            // EventBridge interval. Locked decision in tasks/plan.md §7.
            reservedConcurrentExecutions: 1,
            code: lambda.Code.fromAsset('./lambdas/poller', {
                exclude: [
                    '.venv/**', '.venv', '.venv-tests/**',
                    '*.pyc', '__pycache__/**', '.idea/**',
                    'tests/**', 'dev-requirements.txt',
                ],
            }),
            environment: {
                WATCHES_TABLE_NAME:      props.watchesTable.tableName,
                FARE_HISTORY_TABLE_NAME: props.fareHistoryTable.tableName,
                // T2: per-watch MCP calls. Same shared HS256 secret the
                // agent uses; the mcp-authorizer Lambda re-verifies on the
                // way in and the MCP server re-verifies in-handler as
                // defense in depth (see threat-model boundary [2]).
                JWT_SIGNATURE_SECRET:   props.jwtSignatureSecret,
                FLIGHTS_MCP_ENDPOINT:   props.flightsMcpEndpoint,
                HOTELS_MCP_ENDPOINT:    props.hotelsMcpEndpoint,
                // T6: Bedrock Haiku 4.5 decision call (ADR 0004).
                BEDROCK_MODEL_ID: bedrockModelId,
                BEDROCK_MODE:     bedrockMode,
                // Powertools log-level / service-name / metrics-namespace.
                POWERTOOLS_SERVICE_NAME: 'trip-tracker-poller',
                LOG_LEVEL: 'INFO',
            },
        });

        // T1 grant: read-only on Watches.
        // T3 grant: read+write on FareHistory (poller writes snapshots,
        // and the gate logic in T4 reads the 30-day window for anomaly
        // detection).
        props.watchesTable.grantReadData(pollerFn);
        props.fareHistoryTable.grantReadWriteData(pollerFn);

        // T6 grant: bedrock:InvokeModel resource-scoped to the chosen
        // foundation-model ARN. NOT `Resource: '*'` and NOT `bedrock:*`
        // — the poller never needs to enumerate models or invoke
        // anything else. If a deploy ever switches to a cross-region
        // inference profile, supply its full ARN explicitly via the
        // `bedrockInferenceProfileArn` context (the format is
        // account-scoped + uses `inference-profile/...` resource type,
        // which we can't synthesise from the model id alone).
        const region = Stack.of(this).region;
        const bedrockResources = [`arn:aws:bedrock:${region}::foundation-model/${bedrockModelId}`];
        const inferenceProfileArn = scope.node.tryGetContext('bedrockInferenceProfileArn');
        if (inferenceProfileArn) bedrockResources.push(inferenceProfileArn);
        pollerFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['bedrock:InvokeModel'],
            resources: bedrockResources,
        }));

        // EventBridge schedule — enabled at T5. Cadence comes from the
        // `pollIntervalMinutes` CDK context (default 240 = 4h, design-spec §5).
        // Override at deploy time:  cdk deploy -c pollIntervalMinutes=15
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

        // Expose for downstream slices to add IAM grants / env vars.
        this.pollerFn = pollerFn;
        this.scheduleRule = rule;
    }
}

module.exports = PollerServerConstruct;
