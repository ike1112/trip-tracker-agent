/**
 * Tests for ObservabilityDashboardConstruct.
 *
 * Five groups:
 *   A. Construct loads + dashboard resource shape.
 *   B. Namespace + metric-name constants + widget count.
 *   C. Per-Lambda metric-dimension shape assertions (substring is not
 *      sufficient — a widget label could carry the function name while
 *      the metric dimension is wrong).
 *   D. Determinism — two synth passes with identical inputs produce
 *      byte-identical dashboard bodies. Protects the cdk-diff-clean
 *      invariant.
 *   F. Full-stack synth assertions — instantiate
 *      TripTrackerStack itself, parse the synthesised
 *      DashboardBody Fn::Join, and assert metric-dimension shape for
 *      every expected Lambda + API.
 *
 * Cross-language sync (Group E) lives on the Python side as
 * `lambdas/poller/tests/test_metrics_constants_sync.py`.
 */

const { App, Stack } = require('aws-cdk-lib');
const lambda  = require('aws-cdk-lib/aws-lambda');
const apigw   = require('aws-cdk-lib/aws-apigateway');
const ObservabilityDashboardConstruct = require('../lib/observability-dashboard');
const { POLLER_METRIC_NAMESPACE, POLLER_METRIC_NAMES } = require('../lib/observability-dashboard');

// CFN pseudo parameters resolve at deploy time. Substitute a placeholder
// during tests so DashboardBody can JSON.parse and metric arrays can be
// inspected.
const PSEUDO_PARAMS = {
    'AWS::Region': 'us-east-1',
    'AWS::AccountId': '123456789012',
    'AWS::Partition': 'aws',
};

// Build the dashboard against mock Lambdas + APIs. Returns
// { app, stack, dashboard, template, dashboardBody }.
function buildIsolatedDashboard(overrides = {}) {
    const app = new App();
    const stack = new Stack(app, 'IsolatedDashboardStack');
    const archFn = (id, fnName) => new lambda.Function(stack, id, {
        functionName: fnName,
        runtime: lambda.Runtime.NODEJS_22_X,
        handler: 'index.handler',
        code: lambda.Code.fromInline('exports.handler = async () => ({});'),
        architecture: lambda.Architecture.ARM_64,
    });
    const restApi = (id, name) => {
        const api = new apigw.RestApi(stack, id, {
            restApiName: name,
            endpointTypes: [apigw.EndpointType.REGIONAL],
            deploy: true,
        });
        // CDK refuses to synth a RestApi with zero methods. The dashboard
        // construct only reads `api.restApiName`, but synth-time validation
        // walks the construct tree. Attach a trivial GET on / so the API
        // is well-formed.
        api.root.addMethod('GET', new apigw.MockIntegration({
            integrationResponses: [{ statusCode: '200' }],
            passthroughBehavior: apigw.PassthroughBehavior.NEVER,
            requestTemplates: { 'application/json': '{ "statusCode": 200 }' },
        }), { methodResponses: [{ statusCode: '200' }] });
        return api;
    };
    const props = {
        pollerFunction:            archFn('Poller', 'trip-tracker-poller'),
        notifierFunction:          archFn('Notifier', 'trip-tracker-notifier'),
        agentFunction:              archFn('Agent', 'travel-agent-on-lambda'),
        flightsMcpFunction:        archFn('FlightsMcp', 'flights-mcp-server'),
        hotelsMcpFunction:         archFn('HotelsMcp', 'hotels-mcp-server'),
        flightsAuthorizerFunction: archFn('FlightsAuth', 'flights-mcp-server-authorizer'),
        hotelsAuthorizerFunction:  archFn('HotelsAuth', 'hotels-mcp-server-authorizer'),
        agentAuthorizerFunction:   archFn('AgentAuth', 'travel-agent-authorizer'),
        flightsMcpApi:             restApi('FlightsApi', 'flights-mcp-api'),
        hotelsMcpApi:              restApi('HotelsApi', 'hotels-mcp-api'),
        agentApi:                  restApi('AgentApi', 'travel-agent-api'),
        notifierSenderEmail:       'alerts@example.com',
        ...overrides,
    };
    const construct = new ObservabilityDashboardConstruct(stack, 'Dashboard', props);
    const tmpl = app.synth().getStackByName('IsolatedDashboardStack').template;
    return { app, stack, dashboard: construct.dashboard, template: tmpl, props };
}

// Resolve DashboardBody Fn::Join into a string, substituting Lambda
// FunctionName + ApiGateway Name + CFN pseudo parameters. Returns parsed
// widget JSON. Throws if any ref can't be resolved.
function parseDashboardBody(template) {
    const resources = template.Resources || {};
    const dashEntries = Object.values(resources).filter(r => r.Type === 'AWS::CloudWatch::Dashboard');
    if (dashEntries.length !== 1) throw new Error(`expected exactly 1 dashboard resource, got ${dashEntries.length}`);
    const fnJoin = dashEntries[0].Properties.DashboardBody['Fn::Join'];
    if (!fnJoin) throw new Error('DashboardBody is not an Fn::Join');
    const parts = fnJoin[1];
    const resourceNames = {};
    for (const [id, r] of Object.entries(resources)) {
        if (r.Type === 'AWS::Lambda::Function' && r.Properties?.FunctionName) resourceNames[id] = r.Properties.FunctionName;
        else if (r.Type === 'AWS::ApiGateway::RestApi' && r.Properties?.Name) resourceNames[id] = r.Properties.Name;
    }
    const resolvedParts = parts.map(p => {
        if (typeof p === 'string') return p;
        if (p.Ref) {
            if (resourceNames[p.Ref]) return resourceNames[p.Ref];
            if (PSEUDO_PARAMS[p.Ref]) return PSEUDO_PARAMS[p.Ref];
        }
        if (p['Fn::GetAtt'] && resourceNames[p['Fn::GetAtt'][0]]) return resourceNames[p['Fn::GetAtt'][0]];
        throw new Error(`unresolved ref in DashboardBody: ${JSON.stringify(p)}`);
    });
    const joined = resolvedParts.join('');
    if (joined.includes('undefined')) throw new Error('DashboardBody contains literal "undefined"');
    return { rawJoined: joined, dashJson: JSON.parse(joined), dashboardResource: dashEntries[0] };
}

function collectMetrics(dashJson) {
    const all = [];
    for (const w of (dashJson.widgets || [])) {
        const ms = (w.properties && w.properties.metrics) || [];
        for (const m of ms) all.push(m);
    }
    return all;
}

function hasMetricDimension(metricEntries, namespace, dimKey, dimValue) {
    return metricEntries.some(m => {
        if (!Array.isArray(m) || m[0] !== namespace) return false;
        for (let i = 2; i < m.length - 1; i++) {
            if (m[i] === dimKey && m[i + 1] === dimValue) return true;
        }
        return false;
    });
}

const EXPECTED_LAMBDAS = [
    'trip-tracker-poller',
    'trip-tracker-notifier',
    'travel-agent-on-lambda',
    'flights-mcp-server',
    'hotels-mcp-server',
    'flights-mcp-server-authorizer',
    'hotels-mcp-server-authorizer',
    'travel-agent-authorizer',
];
const EXPECTED_APIS = ['flights-mcp-api', 'hotels-mcp-api', 'travel-agent-api'];

describe('ObservabilityDashboardConstruct', () => {

    describe('Group A — construct + resource shape', () => {
        test('A1_construct_loads_without_throwing', () => {
            expect(() => buildIsolatedDashboard()).not.toThrow();
        });

        test('A2_synthesises_exactly_one_dashboard_resource', () => {
            const { template } = buildIsolatedDashboard();
            const dashes = Object.values(template.Resources).filter(r => r.Type === 'AWS::CloudWatch::Dashboard');
            expect(dashes).toHaveLength(1);
        });

        test('A3_dashboard_name_includes_stack_name', () => {
            const { template } = buildIsolatedDashboard();
            const dash = Object.values(template.Resources).find(r => r.Type === 'AWS::CloudWatch::Dashboard');
            // dashboardName resolves to "trip-tracker-{stackName}"
            expect(dash.Properties.DashboardName).toBe('trip-tracker-IsolatedDashboardStack');
        });

        test('A4_throws_when_required_lambda_prop_is_missing', () => {
            expect(() => buildIsolatedDashboard({ pollerFunction: undefined }))
                .toThrow(/pollerFunction/);
        });

        test('A5_throws_when_required_api_prop_is_missing', () => {
            expect(() => buildIsolatedDashboard({ flightsMcpApi: undefined }))
                .toThrow(/flightsMcpApi/);
        });

        test('A6_throws_when_sender_email_is_missing', () => {
            expect(() => buildIsolatedDashboard({ notifierSenderEmail: undefined }))
                .toThrow(/notifierSenderEmail/);
        });
    });

    describe('Group B — constants + widget count', () => {
        test('B1_NAMESPACE_constant_equals_TripTracker_Poller', () => {
            expect(POLLER_METRIC_NAMESPACE).toBe('TripTracker/Poller');
        });

        test('B2_metric_names_constant_lists_all_four_poller_emf_metrics', () => {
            expect(POLLER_METRIC_NAMES).toEqual([
                'watches_polled',
                'watches_errored',
                'bedrock_decisions_made',
                'alerts_sent',
            ]);
        });

        test('B3_widget_count_equals_seven', () => {
            const { template } = buildIsolatedDashboard();
            const { dashJson } = parseDashboardBody(template);
            expect(dashJson.widgets).toHaveLength(7);
        });
    });

    describe('Group C — widget content fidelity (metric-dimension shape, not substring)', () => {
        let metricEntries;
        beforeAll(() => {
            const { template } = buildIsolatedDashboard();
            const { dashJson } = parseDashboardBody(template);
            metricEntries = collectMetrics(dashJson);
        });

        test.each(POLLER_METRIC_NAMES)('C1_poller_widget_includes_%s_metric', (name) => {
            const found = metricEntries.some(m =>
                Array.isArray(m) && m[0] === POLLER_METRIC_NAMESPACE && m[1] === name);
            expect(found).toBe(true);
        });

        describe('C5_lambda_invocations_widget_metric_shape', () => {
            test.each(EXPECTED_LAMBDAS)('includes %s as Invocations dimension', (fnName) => {
                const found = metricEntries.some(m =>
                    Array.isArray(m) && m[0] === 'AWS/Lambda' && m[1] === 'Invocations'
                    && m.includes('FunctionName') && m.includes(fnName));
                expect(found).toBe(true);
            });
        });

        describe('C6_lambda_errors_widget_metric_shape', () => {
            test.each(EXPECTED_LAMBDAS)('includes %s as Errors dimension', (fnName) => {
                const found = metricEntries.some(m =>
                    Array.isArray(m) && m[0] === 'AWS/Lambda' && m[1] === 'Errors'
                    && m.includes('FunctionName') && m.includes(fnName));
                expect(found).toBe(true);
            });
        });

        describe('C7_lambda_duration_widget_metric_shape', () => {
            test.each(EXPECTED_LAMBDAS)('includes %s as Duration dimension', (fnName) => {
                const found = metricEntries.some(m =>
                    Array.isArray(m) && m[0] === 'AWS/Lambda' && m[1] === 'Duration'
                    && m.includes('FunctionName') && m.includes(fnName));
                expect(found).toBe(true);
            });
        });

        test('C8_ses_widget_dimension_filters_to_configured_sender_identity', () => {
            const found = metricEntries.some(m =>
                Array.isArray(m) && m[0] === 'AWS/SES'
                && m.includes('Source') && m.includes('alerts@example.com'));
            expect(found).toBe(true);
        });
    });

    describe('Group D — determinism', () => {
        test('D1_two_synth_passes_with_same_inputs_produce_identical_dashboard_body', () => {
            const { template: firstTemplate } = buildIsolatedDashboard();
            const { template: secondTemplate } = buildIsolatedDashboard();
            const firstDash = Object.values(firstTemplate.Resources).find(r => r.Type === 'AWS::CloudWatch::Dashboard');
            const secondDash = Object.values(secondTemplate.Resources).find(r => r.Type === 'AWS::CloudWatch::Dashboard');
            // DashboardBody is an Fn::Join — comparing the structured value
            // catches both literal-string drift and ref-ordering drift.
            expect(JSON.stringify(firstDash.Properties.DashboardBody))
                .toBe(JSON.stringify(secondDash.Properties.DashboardBody));
        });
    });

    describe('Group F — full-stack synth assertions', () => {
        let metricEntries;
        let joined;

        beforeAll(() => {
            // Skip Docker-backed AgentConstruct DependenciesLayer bundling;
            // the dashboard wiring is what's under test, not the Python
            // wheel layer.
            process.env.DUFFEL_API_KEY = 'stub';
            process.env.LITEAPI_API_KEY = 'stub';
            const { TripTrackerStack } = require('../lib/trip-tracker-stack');
            const app = new App({ context: {
                'aws:cdk:bundling-stacks': [],
                mcpMode: 'fixture',
                bedrockModelId: 'claude-haiku-4-5-20251001',
                bedrockMode: 'stub',
                notifierSenderEmail: 'test@example.com',
                notifierRecipientEmail: 'me@example.com',
                sesMode: 'stub',
            }});
            const stack = new TripTrackerStack(app, 'FullStackTest', {});
            const tmpl = app.synth().getStackByName('FullStackTest').template;
            const parsed = parseDashboardBody(tmpl);
            joined = parsed.rawJoined;
            metricEntries = collectMetrics(parsed.dashJson);
        });

        test.each(EXPECTED_LAMBDAS)('F1_lambda_metric_dimension_for_%s', (fnName) => {
            const hasInvocations = hasMetricDimension(metricEntries, 'AWS/Lambda', 'FunctionName', fnName);
            expect(hasInvocations).toBe(true);
        });

        test.each(EXPECTED_APIS)('F2_apigateway_metric_dimension_for_%s', (apiName) => {
            const hasMetric = hasMetricDimension(metricEntries, 'AWS/ApiGateway', 'ApiName', apiName);
            expect(hasMetric).toBe(true);
        });

        test('F3_no_undefined_in_any_metric_entry', () => {
            for (const m of metricEntries) {
                if (!Array.isArray(m)) continue;
                for (const slot of m) {
                    if (typeof slot === 'string') expect(slot).not.toBe('undefined');
                }
            }
            // Belt-and-braces: the resolved body string shouldn't contain "undefined" anywhere.
            expect(joined).not.toMatch(/"undefined"/);
        });
    });
});
