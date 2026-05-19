/**
 * Tests for DataStoresConstruct — the Watches `status-index` GSI
 * (ADR 0007) and the poller's IAM coverage of it.
 *
 *   J-A  GSI shape: WatchesTable has exactly the status-index GSI
 *        (PK status/HASH, Projection ALL) + status in AttributeDefinitions.
 *   J-C  No regression: WatchesTable has exactly one GSI; FareHistory
 *        has none.
 *   J-B  Full-stack synth: the poller function's role has one IAM
 *        statement granting dynamodb:Query whose Resource covers BOTH
 *        the Watches table ARN AND its /index/* ARN (locks §0 #5/#12 —
 *        a GSI Query needs the index ARN or it AccessDenies at runtime).
 */

const { App, Stack } = require('aws-cdk-lib');
const { Template, Match } = require('aws-cdk-lib/assertions');
const DataStoresConstruct = require('../lib/data-stores');

// Locate the two DDB tables by key schema rather than logical id:
// Watches is PK userId / SK watchId; FareHistory is PK watchId / SK
// timestamp. Returns { watches, fareHistory } Properties blocks.
function tablesByRole(template) {
    const props = Object.values(template.findResources('AWS::DynamoDB::Table'))
        .map((r) => r.Properties);
    const keyNames = (p) => (p.KeySchema || []).map((k) => k.AttributeName);
    const watches = props.find((p) => keyNames(p).includes('userId'));
    const fareHistory = props.find((p) => keyNames(p).includes('timestamp'));
    return { watches, fareHistory };
}

describe('DataStoresConstruct — Group J-A: status-index GSI shape', () => {
    let template;
    beforeAll(() => {
        const app = new App();
        const stack = new Stack(app, 'IsolatedDataStores');
        new DataStoresConstruct(stack, 'DataStores');
        template = Template.fromStack(stack);
    });

    test('WatchesTable has a status-index GSI: PK status/HASH, Projection ALL', () => {
        const { watches } = tablesByRole(template);
        expect(watches).toBeDefined();
        const gsis = watches.GlobalSecondaryIndexes;
        expect(gsis).toHaveLength(1);
        expect(gsis[0]).toEqual({
            IndexName: 'status-index',
            KeySchema: [{ AttributeName: 'status', KeyType: 'HASH' }],
            Projection: { ProjectionType: 'ALL' },
        });
    });

    test('status is declared in AttributeDefinitions as S', () => {
        const { watches } = tablesByRole(template);
        expect(watches.AttributeDefinitions).toEqual(
            expect.arrayContaining([{ AttributeName: 'status', AttributeType: 'S' }]),
        );
    });
});

describe('DataStoresConstruct — Group J-C: no GSI regression', () => {
    test('WatchesTable has exactly one GSI; FareHistory has none', () => {
        const app = new App();
        const stack = new Stack(app, 'IsolatedDataStores2');
        new DataStoresConstruct(stack, 'DataStores');
        const template = Template.fromStack(stack);

        template.resourceCountIs('AWS::DynamoDB::Table', 2);
        const { watches, fareHistory } = tablesByRole(template);
        expect(watches.GlobalSecondaryIndexes).toHaveLength(1);
        expect(fareHistory.GlobalSecondaryIndexes).toBeUndefined();
    });
});

describe('DataStoresConstruct — Group J-B: poller can Query the GSI', () => {
    afterAll(() => jest.resetModules());

    test('the poller role grants dynamodb:Query on the table AND its /index/*', () => {
        process.env.DUFFEL_API_KEY = 'stub';
        process.env.LITEAPI_API_KEY = 'stub';
        jest.resetModules();
        // Re-require AFTER resetModules so Template/Match share the same
        // aws-cdk-lib instance as the freshly-required stack (the
        // cross-instance matcher hazard documented in budget-alarm.test.js).
        const { App } = require('aws-cdk-lib');
        const { Template } = require('aws-cdk-lib/assertions');
        const { TripTrackerStack } = require('../lib/trip-tracker-stack');
        const app = new App({ context: {
            'aws:cdk:bundling-stacks': [],
            mcpMode: 'fixture',
            bedrockModelId: 'claude-haiku-4-5-20251001',
            bedrockMode: 'stub',
            notifierSenderEmail: 's@example.com',
            notifierRecipientEmail: 'me@example.com',
            sesMode: 'stub',
        } });
        const stack = new TripTrackerStack(app, 'GsiWiringStack', {});
        const template = Template.fromStack(stack);

        // Identify the poller Lambda by its poller-only env vars, then
        // its execution role, then the IAM policy attached to that role.
        const fns = template.findResources('AWS::Lambda::Function');
        const pollerEntry = Object.entries(fns).find(([, f]) => {
            const v = (f.Properties.Environment || {}).Variables || {};
            return 'WATCHES_TABLE_NAME' in v && 'POLLER_JWT_SECRET_ARN' in v;
        });
        expect(pollerEntry).toBeDefined();
        const roleRef = pollerEntry[1].Properties.Role['Fn::GetAtt'][0];

        const policies = Object.values(template.findResources('AWS::IAM::Policy'));
        const pollerPolicies = policies.filter((p) =>
            (p.Properties.Roles || []).some((r) => r.Ref === roleRef),
        );
        expect(pollerPolicies.length).toBeGreaterThan(0);

        const stmts = pollerPolicies.flatMap(
            (p) => p.Properties.PolicyDocument.Statement,
        );
        const serialise = (x) => JSON.stringify(x);
        const ddbQuery = stmts.find((s) => {
            const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
            return actions.includes('dynamodb:Query');
        });
        expect(ddbQuery).toBeDefined();

        const resources = Array.isArray(ddbQuery.Resource)
            ? ddbQuery.Resource
            : [ddbQuery.Resource];
        const blob = serialise(resources);
        // The base Watches table ARN (a GetAtt on the table resource)...
        expect(blob).toMatch(/"Fn::GetAtt":\[".*[Ww]atches.*","Arn"\]/);
        // ...and the index ARN (grantReadData appends a /index/* Join).
        expect(blob).toContain('/index/*');
    });
});
