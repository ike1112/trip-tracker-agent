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
    const { TripTrackerStack } = require('../lib/trip-tracker-stack');
    const app = new App({ context: {
        'aws:cdk:bundling-stacks': [],
        mcpMode: 'fixture',
        bedrockModelId: 'claude-haiku-4-5-20251001',
        bedrockMode: 'stub',
        notifierSenderEmail: 'test@example.com',
        notifierRecipientEmail: 'me@example.com',
        sesMode: 'stub',
    } });
    const stack = new TripTrackerStack(app, 'WiringTestStack', {});
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

    // Per-Lambda grant assertion, not a stack-wide floor count: resolve
    // each secret-reading Lambda → its role → the IAM policies on that
    // role, and assert a GetSecretValue statement exists. A floor count
    // stays green if one Lambda loses its grant while another gains one;
    // this catches the specific Lambda losing it.
    test('B7 every secret-reading Lambda has a GetSecretValue grant on its own role', () => {
        const resources = tmpl.Resources || {};

        // Map each Lambda function name → its role's logical id.
        const roleOfFn = (fnName) => {
            const fn = fnByName(fnName);
            const roleArn = fn?.Properties?.Role; // { 'Fn::GetAtt': [RoleLogicalId, 'Arn'] }
            return roleArn?.['Fn::GetAtt']?.[0] ?? null;
        };
        // Does any IAM policy bound to roleLogicalId carry GetSecretValue
        // (resource-scoped, never '*')?
        const roleHasGetSecret = (roleLogicalId) => {
            for (const r of Object.values(resources)) {
                if (r.Type !== 'AWS::IAM::Policy') continue;
                const boundRoles = (r.Properties?.Roles || []).map((x) => x?.Ref);
                if (!boundRoles.includes(roleLogicalId)) continue;
                for (const s of r.Properties?.PolicyDocument?.Statement || []) {
                    const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
                    if (actions.some((a) => typeof a === 'string' && a.startsWith('secretsmanager:GetSecretValue'))) {
                        const res = Array.isArray(s.Resource) ? s.Resource : [s.Resource];
                        expect(res).not.toContain('*');
                        return true;
                    }
                }
            }
            return false;
        };

        const secretReaders = [
            'travel-agent-on-lambda',
            'flights-mcp-server',
            'flights-mcp-server-authorizer',
            'hotels-mcp-server',
            'hotels-mcp-server-authorizer',
        ];
        for (const fnName of secretReaders) {
            const role = roleOfFn(fnName);
            expect(role).toBeTruthy();
            expect(roleHasGetSecret(role)).toBe(true);
        }
        // The poller reads its secret too; it has no fixed FunctionName
        // assertion above, so check by name pattern.
        const poller = allLambdas().find(
            (r) => /poller/i.test(JSON.stringify(r.Properties.FunctionName || '')),
        );
        const pollerRole = poller?.Properties?.Role?.['Fn::GetAtt']?.[0];
        expect(pollerRole).toBeTruthy();
        expect(roleHasGetSecret(pollerRole)).toBe(true);
    });

    test('B8 agent Lambda has AGENT_BEDROCK_MODEL_ID', () => {
        expect(env('travel-agent-on-lambda').AGENT_BEDROCK_MODEL_ID).toBeDefined();
    });
});
