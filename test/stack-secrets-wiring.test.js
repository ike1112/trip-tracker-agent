/**
 * Group B — Stack secret wiring (ADR 0006).
 *
 * Full-stack synth (Docker skipped). Assert every Lambda gets the right
 * secret-ARN env vars, the old shared-secret env var is gone everywhere,
 * the server + authorizer Lambdas can read both secrets, and no Secrets
 * Manager grant uses a Resource wildcard.
 */
const { App } = require('aws-cdk-lib');

let tmpl;
beforeAll(() => {
    process.env.DUFFEL_API_KEY = 'stub';
    process.env.LITEAPI_API_KEY = 'stub';
    jest.resetModules();
    const { StrandsAgentOnLambdaStack } = require('../lib/strands-agent-on-lambda-stack');
    const app = new App({ context: {
        'aws:cdk:bundling-stacks': [],
        mcpMode: 'fixture',
        bedrockModelId: 'claude-haiku-4-5-20251001',
        bedrockMode: 'stub',
        notifierSenderEmail: 'test@example.com',
        notifierRecipientEmail: 'me@example.com',
        sesMode: 'stub',
    } });
    const stack = new StrandsAgentOnLambdaStack(app, 'WiringTestStack', {});
    tmpl = app.synth().getStackByName('WiringTestStack').template;
});

function fnByName(name) {
    for (const r of Object.values(tmpl.Resources || {})) {
        if (r.Type === 'AWS::Lambda::Function' && r.Properties?.FunctionName === name) return r;
    }
    return null;
}
const env = (name) => fnByName(name)?.Properties?.Environment?.Variables || {};
const allLambdas = () =>
    Object.values(tmpl.Resources || {}).filter((r) => r.Type === 'AWS::Lambda::Function');

describe('Group B — stack secret wiring', () => {
    test('B1 agent Lambda has AGENT_JWT_SECRET_ARN', () => {
        expect(env('travel-agent-on-lambda').AGENT_JWT_SECRET_ARN).toBeDefined();
    });

    test('B2 poller Lambda has POLLER_JWT_SECRET_ARN', () => {
        const poller = allLambdas().find(
            (r) => /poller/i.test(JSON.stringify(r.Properties.FunctionName || '')),
        );
        expect(poller.Properties.Environment.Variables.POLLER_JWT_SECRET_ARN).toBeDefined();
    });

    test('B3 flights authorizer AND server have both secret ARN env vars', () => {
        for (const fn of ['flights-mcp-server', 'flights-mcp-server-authorizer']) {
            expect(env(fn).AGENT_JWT_SECRET_ARN).toBeDefined();
            expect(env(fn).POLLER_JWT_SECRET_ARN).toBeDefined();
        }
    });

    test('B4 hotels authorizer AND server have both secret ARN env vars', () => {
        for (const fn of ['hotels-mcp-server', 'hotels-mcp-server-authorizer']) {
            expect(env(fn).AGENT_JWT_SECRET_ARN).toBeDefined();
            expect(env(fn).POLLER_JWT_SECRET_ARN).toBeDefined();
        }
    });

    test('B5 no Lambda carries the old JWT_SIGNATURE_SECRET env var', () => {
        for (const fn of allLambdas()) {
            const v = fn.Properties?.Environment?.Variables || {};
            expect(Object.keys(v)).not.toContain('JWT_SIGNATURE_SECRET');
        }
    });

    test('B6 no secretsmanager:GetSecretValue grant uses Resource:*', () => {
        for (const r of Object.values(tmpl.Resources || {})) {
            if (r.Type !== 'AWS::IAM::Policy') continue;
            for (const s of r.Properties?.PolicyDocument?.Statement || []) {
                const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
                if (!actions.some((a) => typeof a === 'string' && a.startsWith('secretsmanager:GetSecretValue'))) continue;
                const res = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
                expect(res).not.toContain('*');
            }
        }
    });

    test('B7 flights+hotels server Lambdas can read both secrets (GetSecretValue present)', () => {
        // Two secrets × two server Lambdas → the policy doc must carry
        // secretsmanager:GetSecretValue statements (resource-scoped, not *).
        let getSecretStmts = 0;
        for (const r of Object.values(tmpl.Resources || {})) {
            if (r.Type !== 'AWS::IAM::Policy') continue;
            for (const s of r.Properties?.PolicyDocument?.Statement || []) {
                const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
                if (actions.some((a) => typeof a === 'string' && a.startsWith('secretsmanager:GetSecretValue'))) {
                    getSecretStmts += 1;
                }
            }
        }
        // agent(1) + poller(1) + flights server+authorizer(2) + hotels
        // server+authorizer(2), each reading 1-2 secrets. At minimum the
        // server Lambdas contribute, so there must be several.
        expect(getSecretStmts).toBeGreaterThanOrEqual(6);
    });

    test('B8 agent Lambda has AGENT_BEDROCK_MODEL_ID', () => {
        expect(env('travel-agent-on-lambda').AGENT_BEDROCK_MODEL_ID).toBeDefined();
    });
});
