const cloudwatch = require('aws-cdk-lib/aws-cloudwatch');
const { Duration, Stack } = require('aws-cdk-lib');
const { Construct } = require('constructs');

// Keep in sync with lambdas/poller/metrics.py:NAMESPACE and the
// WATCHES_POLLED / WATCHES_ERRORED / BEDROCK_DECISIONS_MADE / ALERTS_SENT
// constants there. The cross-language sync gate at
// lambdas/poller/tests/test_metrics_constants_sync.py reads these as
// text and asserts they match the Python module's exports — any drift
// fails the gate before merge.
const POLLER_METRIC_NAMESPACE = 'TripTracker/Poller';
const POLLER_METRIC_NAMES = [
    'watches_polled',
    'watches_errored',
    'bedrock_decisions_made',
    'alerts_sent',
];

/**
 * ObservabilityDashboardConstruct — provisions the trip-tracker single-
 * pane-of-glass CloudWatch dashboard.
 *
 * Design notes:
 *
 * - One dashboard per stack. The name suffixes the stack name so dev /
 *   staging / prod deploys coexist without collision.
 *
 * - Seven widgets in a deterministic order so `cdk diff` stays empty
 *   across re-deploys with unchanged inputs:
 *
 *     1. Poller EMF metrics (four `TripTracker/Poller` counters).
 *     2. Lambda invocations (every primary + authorizer Lambda).
 *     3. Lambda errors    (same set).
 *     4. Lambda duration p99 (same set).
 *     5. API Gateway 4xx + 5xx (every API Gateway).
 *     6. SES Send / Bounce / Complaint (scoped to the sender identity).
 *     7. Placeholder row for the future alarms-overview widget.
 *
 * - Eight Lambda metric sources are accepted: five primary functions
 *   (poller, notifier, agent, flights-mcp, hotels-mcp) plus three JWT
 *   authorizer functions (flights, hotels, agent). The authorizers
 *   are first-class on the dashboard so an auth-boundary failure
 *   (HS256 verify crash, Cognito JWKS fetch failure, OOM) is
 *   distinguishable from a downstream Lambda failure — otherwise the
 *   API GW 4xx/5xx widget alone can't tell those cases apart.
 *
 * - All metrics use `cloudwatch.Metric` directly (not
 *   `Metric.fromMetricName` — that factory doesn't exist in
 *   aws-cdk-lib v2.196).
 *
 * - No alarms attached here; alarm scope is the separate production-
 *   readiness close-out work. Widget 7 is a placeholder row that
 *   labels where alarm summary widgets will go.
 */
class ObservabilityDashboardConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        const {
            pollerFunction,
            notifierFunction,
            agentFunction,
            flightsMcpFunction,
            hotelsMcpFunction,
            flightsAuthorizerFunction,
            hotelsAuthorizerFunction,
            agentAuthorizerFunction,
            flightsMcpApi,
            hotelsMcpApi,
            agentApi,
            notifierSenderEmail,
        } = props;

        // Synth-time validation — fail loud rather than silently graphing
        // an undefined ref. Every prop is required.
        const requiredFunctionProps = [
            'pollerFunction', 'notifierFunction', 'agentFunction',
            'flightsMcpFunction', 'hotelsMcpFunction',
            'flightsAuthorizerFunction', 'hotelsAuthorizerFunction',
            'agentAuthorizerFunction',
        ];
        for (const name of requiredFunctionProps) {
            if (!props[name]) {
                throw new Error(`ObservabilityDashboardConstruct: required prop "${name}" is missing or undefined`);
            }
        }
        const requiredApiProps = ['flightsMcpApi', 'hotelsMcpApi', 'agentApi'];
        for (const name of requiredApiProps) {
            if (!props[name]) {
                throw new Error(`ObservabilityDashboardConstruct: required prop "${name}" is missing or undefined`);
            }
        }
        if (!notifierSenderEmail || typeof notifierSenderEmail !== 'string') {
            throw new Error('ObservabilityDashboardConstruct: notifierSenderEmail (string) is required');
        }

        // Lambdas in a deterministic order (poller, notifier, agent,
        // flights-mcp, hotels-mcp, flights-authorizer, hotels-authorizer,
        // agent-authorizer). Same order across all three Lambda widgets.
        const lambdas = [
            pollerFunction,
            notifierFunction,
            agentFunction,
            flightsMcpFunction,
            hotelsMcpFunction,
            flightsAuthorizerFunction,
            hotelsAuthorizerFunction,
            agentAuthorizerFunction,
        ];
        // APIs in a deterministic order (flights-mcp-api, hotels-mcp-api,
        // travel-agent-api).
        const apis = [flightsMcpApi, hotelsMcpApi, agentApi];

        const period = Duration.minutes(5);

        // Widget 1 — poller EMF metrics. One line per named counter.
        const pollerMetricsWidget = new cloudwatch.GraphWidget({
            title: 'Poller — EMF counters (5m)',
            width: 24,
            height: 6,
            period,
            left: POLLER_METRIC_NAMES.map((name) => new cloudwatch.Metric({
                namespace: POLLER_METRIC_NAMESPACE,
                metricName: name,
                statistic: 'Sum',
                period,
                label: name,
            })),
        });

        // Widget 2 — Lambda invocations.
        const lambdaInvocationsWidget = new cloudwatch.GraphWidget({
            title: 'Lambda — invocations (5m)',
            width: 24,
            height: 6,
            period,
            left: lambdas.map((fn) => new cloudwatch.Metric({
                namespace: 'AWS/Lambda',
                metricName: 'Invocations',
                statistic: 'Sum',
                period,
                dimensionsMap: { FunctionName: fn.functionName },
                label: fn.functionName,
            })),
        });

        // Widget 3 — Lambda errors.
        const lambdaErrorsWidget = new cloudwatch.GraphWidget({
            title: 'Lambda — errors (5m)',
            width: 24,
            height: 6,
            period,
            left: lambdas.map((fn) => new cloudwatch.Metric({
                namespace: 'AWS/Lambda',
                metricName: 'Errors',
                statistic: 'Sum',
                period,
                dimensionsMap: { FunctionName: fn.functionName },
                label: fn.functionName,
            })),
        });

        // Widget 4 — Lambda duration p99.
        const lambdaDurationWidget = new cloudwatch.GraphWidget({
            title: 'Lambda — duration p99 (5m)',
            width: 24,
            height: 6,
            period,
            left: lambdas.map((fn) => new cloudwatch.Metric({
                namespace: 'AWS/Lambda',
                metricName: 'Duration',
                statistic: 'p99',
                period,
                dimensionsMap: { FunctionName: fn.functionName },
                label: fn.functionName,
            })),
        });

        // Widget 5 — API Gateway 4xx + 5xx, all APIs on one chart.
        const apiGatewayErrorsWidget = new cloudwatch.GraphWidget({
            title: 'API Gateway — 4xx + 5xx (5m)',
            width: 24,
            height: 6,
            period,
            left: apis.flatMap((api) => [
                new cloudwatch.Metric({
                    namespace: 'AWS/ApiGateway',
                    metricName: '4XXError',
                    statistic: 'Sum',
                    period,
                    dimensionsMap: { ApiName: api.restApiName },
                    label: `${api.restApiName} 4xx`,
                }),
                new cloudwatch.Metric({
                    namespace: 'AWS/ApiGateway',
                    metricName: '5XXError',
                    statistic: 'Sum',
                    period,
                    dimensionsMap: { ApiName: api.restApiName },
                    label: `${api.restApiName} 5xx`,
                }),
            ]),
        });

        // Widget 6 — SES Send / Bounce / Complaint, scoped to the sender
        // identity. SES dimensions key on `Source` (the verified-identity
        // email). Bounce + Complaint will sit at zero until the v2
        // production-readiness close-out wires the SNS feedback topic;
        // they're on the dashboard now so the v2 work has a place to land.
        const sesDimensions = { Source: notifierSenderEmail };
        const sesWidget = new cloudwatch.GraphWidget({
            title: 'SES — send / bounce / complaint (5m)',
            width: 24,
            height: 6,
            period,
            left: [
                new cloudwatch.Metric({
                    namespace: 'AWS/SES', metricName: 'Send',       statistic: 'Sum', period,
                    dimensionsMap: sesDimensions, label: 'Send',
                }),
                new cloudwatch.Metric({
                    namespace: 'AWS/SES', metricName: 'Bounce',     statistic: 'Sum', period,
                    dimensionsMap: sesDimensions, label: 'Bounce',
                }),
                new cloudwatch.Metric({
                    namespace: 'AWS/SES', metricName: 'Complaint',  statistic: 'Sum', period,
                    dimensionsMap: sesDimensions, label: 'Complaint',
                }),
            ],
        });

        // Widget 7 — alarms-overview placeholder. Text widget that names
        // the section so the production-readiness alarm-bundle work has
        // an obvious slot to plug into without re-arranging the
        // dashboard.
        const alarmsPlaceholderWidget = new cloudwatch.TextWidget({
            markdown: '## Alarms\n\nAlarm overview widgets land here in the production-readiness close-out work.',
            width: 24,
            height: 4,
        });

        const dashboard = new cloudwatch.Dashboard(this, 'TripTrackerDashboard', {
            dashboardName: `trip-tracker-${Stack.of(this).stackName}`,
        });

        // Add widgets in a fixed sequence. Each call is one row.
        dashboard.addWidgets(pollerMetricsWidget);
        dashboard.addWidgets(lambdaInvocationsWidget);
        dashboard.addWidgets(lambdaErrorsWidget);
        dashboard.addWidgets(lambdaDurationWidget);
        dashboard.addWidgets(apiGatewayErrorsWidget);
        dashboard.addWidgets(sesWidget);
        dashboard.addWidgets(alarmsPlaceholderWidget);

        this.dashboard = dashboard;
    }
}

module.exports = ObservabilityDashboardConstruct;
module.exports.POLLER_METRIC_NAMESPACE = POLLER_METRIC_NAMESPACE;
module.exports.POLLER_METRIC_NAMES = POLLER_METRIC_NAMES;
