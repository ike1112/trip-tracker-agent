/**
 * Synth-time + template-shape tests for BudgetAlarmConstruct.
 *
 * Pins the email-regex contract (mirrors test/notifier-server.test.js)
 * AND the synthesised AWS::Budgets::Budget shape. The shape assertions
 * pin Amount/Threshold as NUMBERS (CfnBudget serialises them numeric,
 * not as strings — PRP §0 #1, verified by synth); a regression that
 * widened EMAIL_PATTERN, dropped a notification, or changed the limit
 * would otherwise deploy silently.
 */

const { App, Stack } = require('aws-cdk-lib');
const { Template, Match } = require('aws-cdk-lib/assertions');
const { BudgetAlarmConstruct } = require('../lib/budget-alarm');

let counter = 0;

// Returns a thunk so `expect(build(...)).toThrow()` works (mirrors
// test/notifier-server.test.js). The construct reads its email from
// CDK context via scope.node.tryGetContext, so context is set on the App.
function build(budgetEmail, recipientEmail) {
    const ctx = {};
    if (budgetEmail !== undefined) ctx.budgetAlarmEmail = budgetEmail;
    if (recipientEmail !== undefined) ctx.notifierRecipientEmail = recipientEmail;
    const app = new App({ context: ctx });
    const stack = new Stack(app, `B${counter++}`);
    return () => new BudgetAlarmConstruct(stack, 'B');
}

// Build + synth a template with a known-good email for shape assertions.
function template(budgetEmail = 'cost@example.com', recipientEmail) {
    const ctx = { budgetAlarmEmail: budgetEmail };
    if (recipientEmail !== undefined) ctx.notifierRecipientEmail = recipientEmail;
    const app = new App({ context: ctx });
    const stack = new Stack(app, `BT${counter++}`);
    new BudgetAlarmConstruct(stack, 'B');
    return Template.fromStack(stack);
}

describe('BudgetAlarmConstruct — Group A: email validation', () => {
    describe('accepted shapes', () => {
        test.each([
            'cost@example.com',
            'budget.alerts@example.com',
            'ops+aws@sub.example.co.uk',
            'a_b@example.io',
            'AB123@example.net',
        ])('accepts %s', (email) => {
            expect(build(email)).not.toThrow();
        });
    });

    describe('rejected shapes', () => {
        test.each([
            ['bare domain', 'example.com'],
            ['leading @', '@example.com'],
            ['no TLD', 'cost@example'],
            ['one-char TLD', 'cost@example.c'],
            ['leading hyphen on domain', 'foo@-bar.com'],
            ['trailing hyphen on domain', 'foo@bar-.com'],
            ['double dot in local', 'a..b@example.com'],
            ['leading dot in local', '.a@example.com'],
            ['trailing dot in local', 'a.@example.com'],
            ['CRLF injection', 'foo@bar.com\r\nBcc:x@y.z'],
            ['space', 'foo@bar .com'],
            ['empty string', ''],
            ['undefined', undefined],
        ])('rejects %s', (_label, email) => {
            expect(build(email)).toThrow(/budgetAlarmEmail/);
        });
    });

    test('A3 falls back to notifierRecipientEmail when budgetAlarmEmail unset', () => {
        expect(build(undefined, 'me@example.com')).not.toThrow();
    });

    test('A4 budgetAlarmEmail overrides notifierRecipientEmail in the subscriber', () => {
        const t = template('override@example.com', 'fallback@example.com');
        t.hasResourceProperties('AWS::Budgets::Budget', {
            NotificationsWithSubscribers: Match.arrayWith([
                Match.objectLike({
                    Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'override@example.com' }],
                }),
            ]),
        });
    });

    test('A5 throws when neither email is set', () => {
        expect(build(undefined, undefined)).toThrow(/budgetAlarmEmail/);
    });

    test('A6 a malformed fallback names notifierRecipientEmail, not budgetAlarmEmail', () => {
        // budgetAlarmEmail unset → the bad value came from the fallback;
        // the error must point at the key the operator actually set. The
        // anchored regex fails if the message said "budgetAlarmEmail …".
        expect(build(undefined, 'not-an-email'))
            .toThrow(/^notifierRecipientEmail is required and must look like an email/);
    });
});

describe('BudgetAlarmConstruct — Group B: synthesised budget shape', () => {
    test('B1 exactly one AWS::Budgets::Budget resource', () => {
        template().resourceCountIs('AWS::Budgets::Budget', 1);
    });

    test('B2 COST / MONTHLY / limit 10 USD (Amount is numeric 10)', () => {
        template().hasResourceProperties('AWS::Budgets::Budget', {
            Budget: Match.objectLike({
                BudgetType: 'COST',
                TimeUnit: 'MONTHLY',
                BudgetLimit: { Amount: 10, Unit: 'USD' },
            }),
        });
    });

    test('B3 80% ACTUAL + 100% FORECASTED, numeric thresholds, GREATER_THAN/PERCENTAGE', () => {
        const t = template();
        t.hasResourceProperties('AWS::Budgets::Budget', {
            NotificationsWithSubscribers: Match.arrayWith([
                Match.objectLike({
                    Notification: {
                        NotificationType: 'ACTUAL',
                        ComparisonOperator: 'GREATER_THAN',
                        Threshold: 80,
                        ThresholdType: 'PERCENTAGE',
                    },
                }),
                Match.objectLike({
                    Notification: {
                        NotificationType: 'FORECASTED',
                        ComparisonOperator: 'GREATER_THAN',
                        Threshold: 100,
                        ThresholdType: 'PERCENTAGE',
                    },
                }),
            ]),
        });
        // arrayWith does not pin length — assert exactly two so an
        // erroneously-added third notification is caught here, not only
        // via B4's subscriber path.
        const ns = Object.values(t.findResources('AWS::Budgets::Budget'))[0]
            .Properties.NotificationsWithSubscribers;
        expect(ns).toHaveLength(2);
    });

    test('B4 both notifications carry exactly one EMAIL subscriber with the resolved address', () => {
        const t = template('cost@example.com');
        const budgets = t.findResources('AWS::Budgets::Budget');
        const ns = Object.values(budgets)[0].Properties.NotificationsWithSubscribers;
        expect(ns).toHaveLength(2);
        for (const n of ns) {
            expect(n.Subscribers).toEqual([{ SubscriptionType: 'EMAIL', Address: 'cost@example.com' }]);
        }
    });

    test('B5 budgetName is trip-tracker-monthly-cost', () => {
        template().hasResourceProperties('AWS::Budgets::Budget', {
            Budget: Match.objectLike({ BudgetName: 'trip-tracker-monthly-cost' }),
        });
    });
});

describe('BudgetAlarmConstruct — Group C: full-stack wiring', () => {
    // C1 calls jest.resetModules() mid-test, which makes the top-of-file
    // requires stale relative to the post-reset registry. Resetting again
    // afterwards keeps the isolation order-independent — a future test
    // added after Group C gets a clean registry, not C1's leftovers.
    afterAll(() => jest.resetModules());

    test('C1 the full stack synthesises exactly one $10/MONTHLY/COST budget', () => {
        process.env.DUFFEL_API_KEY = 'stub';
        process.env.LITEAPI_API_KEY = 'stub';
        jest.resetModules();
        // Re-require App/Template/Match AFTER resetModules so they share
        // the same aws-cdk-lib instance as the freshly-required stack —
        // otherwise top-of-file Match objects aren't recognised as
        // matchers by the post-reset Template (cross-instance mismatch).
        const { App } = require('aws-cdk-lib');
        const { Template, Match } = require('aws-cdk-lib/assertions');
        const { StrandsAgentOnLambdaStack } = require('../lib/strands-agent-on-lambda-stack');
        const app = new App({ context: {
            'aws:cdk:bundling-stacks': [],
            mcpMode: 'fixture',
            bedrockModelId: 'claude-haiku-4-5-20251001',
            bedrockMode: 'stub',
            notifierSenderEmail: 's@example.com',
            notifierRecipientEmail: 'me@example.com',
            sesMode: 'stub',
        } });
        const stack = new StrandsAgentOnLambdaStack(app, 'BudgetWiringStack', {});
        const t = Template.fromStack(stack);
        t.resourceCountIs('AWS::Budgets::Budget', 1);
        t.hasResourceProperties('AWS::Budgets::Budget', {
            Budget: Match.objectLike({
                BudgetType: 'COST',
                TimeUnit: 'MONTHLY',
                BudgetLimit: { Amount: 10, Unit: 'USD' },
            }),
            NotificationsWithSubscribers: Match.arrayWith([
                Match.objectLike({
                    Subscribers: [{ SubscriptionType: 'EMAIL', Address: 'me@example.com' }],
                }),
            ]),
        });
    });
});
