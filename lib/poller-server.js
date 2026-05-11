const iam     = require('aws-cdk-lib/aws-iam');
const lambda  = require('aws-cdk-lib/aws-lambda');
const events  = require('aws-cdk-lib/aws-events');
const targets = require('aws-cdk-lib/aws-events-targets');
const { Duration, CfnOutput } = require('aws-cdk-lib');
const { Construct } = require('constructs');

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
 * - The EventBridge rule is created here (so T5 only flips a flag) but is
 *   `enabled: false` in T1. Cadence comes from `pollIntervalMinutes`
 *   context with a 240-minute default. Override at deploy:
 *       cdk deploy -c pollIntervalMinutes=15
 */
class PollerServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // Cadence resolved from CDK context (`-c pollIntervalMinutes=15`),
        // defaulting to 240 (= 4h, design-spec §5).
        const pollIntervalMinutes = scope.node.tryGetContext('pollIntervalMinutes') ?? 240;
        // Lambda timeout scales with watch count: each watch costs at most
        // 2 × MCP_TIMEOUT_SECONDS (15s) sequentially, plus headroom for DDB
        // writes and snapshot composition. Override with -c lambdaTimeoutSeconds=N
        // when watches climb above ~2. Default 60 covers the personal-scale
        // case (1–2 active watches at once); see plan §6 risk 2.
        const lambdaTimeoutSeconds = scope.node.tryGetContext('lambdaTimeoutSeconds') ?? 60;

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

        // EventBridge schedule, disabled at T1. T5 flips `enabled: true`.
        const rule = new events.Rule(this, 'PollerSchedule', {
            schedule: events.Schedule.rate(Duration.minutes(pollIntervalMinutes)),
            enabled: false,
            description: `Trip-tracker poller cron (every ${pollIntervalMinutes} minutes). Disabled until slice 5 T5.`,
        });
        rule.addTarget(new targets.LambdaFunction(pollerFn));

        // Surface the function name + cadence so post-deploy scripts and
        // dashboards can find them without hard-coding.
        new CfnOutput(this, 'PollerFunctionName', { value: pollerFn.functionName });
        new CfnOutput(this, 'PollerCadenceMinutes', { value: String(pollIntervalMinutes) });

        // Expose for downstream slices to add IAM grants / env vars.
        this.pollerFn = pollerFn;
        this.scheduleRule = rule;
    }
}

module.exports = PollerServerConstruct;
