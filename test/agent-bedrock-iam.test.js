/**
 * Group C — Agent Bedrock IAM grant (ADR 0006).
 *
 * The agent model is a `us.`-prefixed geographic inference profile, so
 * the grant must enumerate the foundation-model ARN in all three US
 * destination Regions plus the inference-profile ARN (4 ARNs), never
 * `Resource: '*'`. The `agentBedrockModelId` context override must reach
 * both the IAM ARNs and the AGENT_BEDROCK_MODEL_ID env var; a blank
 * value must throw at synth.
 */
const { App } = require('aws-cdk-lib');

const DEFAULT_MODEL = 'us.anthropic.claude-3-5-haiku-20241022-v1:0';

function synth(extraContext = {}) {
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
        ...extraContext,
    } });
    const stack = new StrandsAgentOnLambdaStack(app, 'IamTestStack', {});
    return app.synth().getStackByName('IamTestStack').template;
}

// Pull every Bedrock InvokeModel* policy statement out of the template.
function bedrockStatements(tmpl) {
    const out = [];
    for (const r of Object.values(tmpl.Resources || {})) {
        if (r.Type !== 'AWS::IAM::Policy') continue;
        for (const s of r.Properties?.PolicyDocument?.Statement || []) {
            const actions = Array.isArray(s.Action) ? s.Action : [s.Action];
            if (actions.some((a) => typeof a === 'string' && a.startsWith('bedrock:InvokeModel'))) {
                out.push(s);
            }
        }
    }
    return out;
}

// Resource entries can be plain strings or Fn::Join arrays. Flatten to
// the literal fragments so we can assert region + arn-type substrings.
function resourceFragments(stmt) {
    const res = Array.isArray(stmt.Resource) ? stmt.Resource : [stmt.Resource];
    return res.map((r) => {
        if (typeof r === 'string') return r;
        if (r && r['Fn::Join']) return r['Fn::Join'][1].map((p) => (typeof p === 'string' ? p : '')).join('');
        return JSON.stringify(r);
    });
}

function agentEnv(tmpl) {
    for (const r of Object.values(tmpl.Resources || {})) {
        if (r.Type !== 'AWS::Lambda::Function') continue;
        if (r.Properties?.FunctionName === 'travel-agent-on-lambda') {
            return r.Properties.Environment.Variables;
        }
    }
    return null;
}

describe('Agent Bedrock IAM grant', () => {
    test('C1 agent role has no Resource:* bedrock grant', () => {
        const stmts = bedrockStatements(synth());
        expect(stmts.length).toBeGreaterThan(0);
        for (const s of stmts) {
            const frags = resourceFragments(s);
            expect(frags).not.toContain('*');
        }
    });

    test('C2 grants InvokeModel on the foundation-model ARN in all 3 US regions', () => {
        const frags = bedrockStatements(synth()).flatMap(resourceFragments).join('\n');
        for (const region of ['us-east-1', 'us-east-2', 'us-west-2']) {
            expect(frags).toContain(`arn:aws:bedrock:${region}::foundation-model/anthropic.claude-3-5-haiku-20241022-v1:0`);
        }
    });

    test('C3 grants InvokeModel on the inference-profile ARN', () => {
        const frags = bedrockStatements(synth()).flatMap(resourceFragments).join('\n');
        expect(frags).toContain(`inference-profile/${DEFAULT_MODEL}`);
    });

    test('C4 grants both InvokeModel and InvokeModelWithResponseStream', () => {
        const stmts = bedrockStatements(synth());
        const actions = new Set(stmts.flatMap((s) => (Array.isArray(s.Action) ? s.Action : [s.Action])));
        expect(actions.has('bedrock:InvokeModel')).toBe(true);
        expect(actions.has('bedrock:InvokeModelWithResponseStream')).toBe(true);
    });

    test('C5 agentBedrockModelId context override reaches IAM ARNs AND the env var', () => {
        const override = 'us.anthropic.claude-sonnet-4-6-v1:0';
        const tmpl = synth({ agentBedrockModelId: override });
        const frags = bedrockStatements(tmpl).flatMap(resourceFragments).join('\n');
        expect(frags).toContain(`inference-profile/${override}`);
        expect(frags).toContain('arn:aws:bedrock:us-east-1::foundation-model/anthropic.claude-sonnet-4-6-v1:0');
        expect(agentEnv(tmpl).AGENT_BEDROCK_MODEL_ID).toBe(override);
    });

    test('C6 agent bedrock grant has exactly 4 ARNs (3 US FM regions + profile)', () => {
        // bedrockStatements() spans the whole stack — the poller has its
        // own single-ARN grant for a different model. Scope to the agent
        // model id so this asserts the agent grant specifically.
        const all = bedrockStatements(synth())
            .flatMap(resourceFragments)
            .filter((f) => f.includes('claude-3-5-haiku-20241022-v1:0'));
        expect(all).toHaveLength(4);
    });

    test('C7 blank agentBedrockModelId throws at synth', () => {
        expect(() => synth({ agentBedrockModelId: '   ' })).toThrow(/agentBedrockModelId/);
    });

    test('C8 non-us. geographic profile throws at synth (loud, not malformed ARNs)', () => {
        expect(() => synth({ agentBedrockModelId: 'eu.anthropic.claude-3-5-sonnet-20241022-v1:0' }))
            .toThrow(/not a us\. geographic/);
    });
});
