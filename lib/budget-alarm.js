const budgets = require('aws-cdk-lib/aws-budgets');
const { Construct } = require('constructs');

// Anchored email-shape regex used for synth-time validation only.
// SOURCE: lib/notifier-server.js (ADR 0005) - copied verbatim, not
// shared. A cross-construct email-validation util is a larger refactor
// than this checklist item; the regex is stable and this construct's
// own tests independently pin the same accept/reject shapes, so a
// divergence fails here (PRP section 9 #3).
const EMAIL_PATTERN =
    /^[A-Za-z0-9_%+\-]+(\.[A-Za-z0-9_%+\-]+)*@[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$/;

/**
 * BudgetAlarmConstruct provisions a single account-level AWS Budget
 * as cheap insurance against a runaway poll loop.
 *
 * Design notes:
 *
 * - One `AWS::Budgets::Budget`: COST, MONTHLY, $10 USD. Account-level
 *   and Region-agnostic; the construct needs no Region/account env and
 *   synthesises under the existing full-stack synth context unchanged.
 *
 * - Two notifications: 80% ACTUAL and 100% FORECASTED. FORECASTED
 *   catches a runaway loop before month-end, when ACTUAL alone would
 *   only fire after the money is spent.
 *
 * - Email subscriber only: no SNS topic, no Budgets action /
 *   auto-remediation. The design spec says email notification; an SNS
 *   hop adds infra for no value at personal scale, and auto-remediation
 *   on a personal stack risks locking the owner out.
 *
 * - No live/stub mode flag. An AWS Budget is free to create and
 *   harmless to deploy, and synth/tests only assert the template.
 *   There is no distinction to gate, so only the email-regex validation
 *   is needed.
 *
 * - Email source: `budgetAlarmEmail` context, falling back to
 *   `notifierRecipientEmail`. Re-validated with EMAIL_PATTERN
 *   regardless of source: the notifier may not be built in a given
 *   synth, so this construct does not assume someone else validated.
 *
 * - Reads its email from CDK context directly, as the notifier reads
 *   its emails from context. It depends on no table, Lambda, or other
 *   construct, so the stack instantiates it with no props.
 *
 * - `budgetName` is a fixed `trip-tracker-monthly-cost`: a stable name
 *   keeps the budget findable in the AWS console and idempotent across
 *   redeploys. CloudFormation would otherwise auto-generate one.
 */
class BudgetAlarmConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        // Both scope.node and this.node walk the CDK tree up to App to
        // resolve context; scope.node mirrors the notifier construct exactly.
        const budgetEmail = scope.node.tryGetContext('budgetAlarmEmail');
        const fallbackEmail = scope.node.tryGetContext('notifierRecipientEmail');
        const email = budgetEmail ?? fallbackEmail;

        // Synth-time validation: fail loud rather than synthesising a
        // budget that silently emails nobody, or a malformed address.
        // The message names the key that actually supplied the bad value
        // so a misconfig points the operator at the right knob.
        if (!email || !EMAIL_PATTERN.test(email)) {
            let which;
            if (budgetEmail !== undefined) which = 'budgetAlarmEmail';
            else if (fallbackEmail !== undefined) which = 'notifierRecipientEmail';
            else which = 'budgetAlarmEmail (or its notifierRecipientEmail fallback)';
            throw new Error(
                `${which} is required and must look like an email; got: ${email}`
            );
        }

        new budgets.CfnBudget(this, 'CostBudget', {
            budget: {
                budgetType: 'COST',
                timeUnit: 'MONTHLY',
                budgetLimit: { amount: 10, unit: 'USD' },
                budgetName: 'trip-tracker-monthly-cost',
            },
            notificationsWithSubscribers: [
                {
                    notification: {
                        notificationType: 'ACTUAL',
                        comparisonOperator: 'GREATER_THAN',
                        threshold: 80,
                        thresholdType: 'PERCENTAGE',
                    },
                    subscribers: [{ subscriptionType: 'EMAIL', address: email }],
                },
                {
                    notification: {
                        notificationType: 'FORECASTED',
                        comparisonOperator: 'GREATER_THAN',
                        threshold: 100,
                        thresholdType: 'PERCENTAGE',
                    },
                    subscribers: [{ subscriptionType: 'EMAIL', address: email }],
                },
            ],
        });
    }
}

module.exports = { BudgetAlarmConstruct };
