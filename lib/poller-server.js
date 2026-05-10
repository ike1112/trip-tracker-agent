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

        const pollerFn = new lambda.Function(this, 'PollerFn', {
            functionName: 'trip-tracker-poller',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: 'app.handler',
            // 60s comfortably covers serial MCP traffic for ~2 watches at
            // 15s each (the realistic case). For larger watch counts the
            // timeout must scale with N — see tasks/slice-5-poller.plan.md
            // §6 risks for the formula.
            timeout: Duration.seconds(60),
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
                // Powertools log-level / service-name / metrics-namespace.
                POWERTOOLS_SERVICE_NAME: 'trip-tracker-poller',
                LOG_LEVEL: 'INFO',
            },
        });

        // T1 grant: read-only on Watches. FareHistory grant lands in T3
        // when the snapshot writer is added.
        props.watchesTable.grantReadData(pollerFn);

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
