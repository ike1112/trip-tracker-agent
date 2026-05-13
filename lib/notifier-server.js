const iam    = require('aws-cdk-lib/aws-iam');
const lambda = require('aws-cdk-lib/aws-lambda');
const { Duration, CfnOutput, Stack } = require('aws-cdk-lib');
const { Construct } = require('constructs');

// Anchored email-shape regex used for synth-time validation only.
// Local part: alphanumerics + . _ % + -, no leading/trailing/consecutive
// dots. Domain: labels of [a-z0-9] with optional internal hyphens, dot-
// separated, ending in a >=2-char alpha TLD. Rejects bare domains
// (forcing IAM scoping to an email SES identity, never a wildcard
// domain identity — see ADR 0005 and the construct comment below).
const EMAIL_PATTERN =
    /^[A-Za-z0-9_%+\-]+(\.[A-Za-z0-9_%+\-]+)*@[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9\-]*[A-Za-z0-9])?)*\.[A-Za-z]{2,}$/;

/**
 * NotifierServerConstruct — provisions the trip-tracker alert-email
 * Lambda. The poller invokes it asynchronously (InvocationType=Event)
 * when `decision.alert === true`, decoupling polling latency from
 * email delivery.
 *
 * Design notes:
 *
 * - Python 3.13 ARM64 with the poller's runtime + powertools layer
 *   shape (boto3 + powertools + xray-sdk). The notifier's own
 *   `requirements.txt` mirrors the poller's; deploy zips stay small.
 *
 * - X-Ray ACTIVE so the trace tells the alert story end-to-end:
 *   EventBridge -> Poller -> Notifier -> SES + DDB.
 *
 * - Reserved concurrency = 5. The notifier is invoked once per
 *   alert-eligible watch per poll cycle; at personal scale that's
 *   under 1/hour. Five concurrent invocations is generous headroom
 *   while still capping a runaway-cron blast radius.
 *
 * - IAM scope: `ses:SendEmail` resource-scoped to the verified
 *   sender identity ARN; DDB `UpdateItem` on the Watches table
 *   (no put, no delete, no scan). Never `ses:*` or `dynamodb:*`,
 *   never `Resource: '*'`.
 *
 * - `SES_MODE` env (live | stub) selected at deploy time via
 *   `-c sesMode=stub`. Defaults to `live`. Bad values throw at
 *   synth so the cold-start ImportError never reaches CloudWatch.
 *
 * - Sender + recipient context values are pattern-validated at
 *   synth time. The verified SES identity itself must be set up
 *   manually in the AWS console (or via a separate Construct);
 *   this construct only grants invoke permission on the named
 *   identity.
 */
class NotifierServerConstruct extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        const senderEmail = scope.node.tryGetContext('notifierSenderEmail');
        const recipientEmail = scope.node.tryGetContext('notifierRecipientEmail');
        const sesMode = scope.node.tryGetContext('sesMode') ?? 'live';

        // Synth-time validation — fail loud rather than waiting for a
        // cold-start crash or a malformed SES request at runtime.
        if (!senderEmail || !EMAIL_PATTERN.test(senderEmail)) {
            throw new Error(
                `notifierSenderEmail context value is required and must look like an email; got: ${senderEmail}`
            );
        }
        if (!recipientEmail || !EMAIL_PATTERN.test(recipientEmail)) {
            throw new Error(
                `notifierRecipientEmail context value is required and must look like an email; got: ${recipientEmail}`
            );
        }
        const ALLOWED_SES_MODES = ['live', 'stub'];
        if (!ALLOWED_SES_MODES.includes(sesMode)) {
            throw new Error(
                `sesMode context value must be one of ${ALLOWED_SES_MODES.join(', ')}; got: ${sesMode}`
            );
        }

        const notifierFn = new lambda.Function(this, 'NotifierFn', {
            functionName: 'trip-tracker-notifier',
            architecture: props.fnArchitecture,
            runtime: lambda.Runtime.PYTHON_3_13,
            handler: 'app.handler',
            timeout: Duration.seconds(30),
            memorySize: 256,
            tracing: lambda.Tracing.ACTIVE,
            // Five concurrent invocations is plenty at personal scale,
            // and bounds the blast radius if something goes wrong (a
            // misconfigured cron, an alert loop).
            reservedConcurrentExecutions: 5,
            code: lambda.Code.fromAsset('./lambdas/notifier', {
                exclude: [
                    '.venv/**', '.venv', '.venv-tests/**',
                    '*.pyc', '__pycache__/**', '.idea/**',
                    'tests/**', 'dev-requirements.txt',
                ],
            }),
            environment: {
                WATCHES_TABLE_NAME:         props.watchesTable.tableName,
                NOTIFIER_SENDER_EMAIL:      senderEmail,
                NOTIFIER_RECIPIENT_EMAIL:   recipientEmail,
                SES_MODE:                   sesMode,
                POWERTOOLS_SERVICE_NAME:    'trip-tracker-notifier',
                LOG_LEVEL:                  'INFO',
            },
        });

        // DDB grant: only the two alert-state fields are written. We
        // can't scope by attribute path in IAM, but `grantUpdateItem`
        // is action-scoped to UpdateItem on the table — no put, no
        // delete, no scan.
        props.watchesTable.grant(notifierFn, 'dynamodb:UpdateItem');

        // SES grant: SendEmail resource-scoped to the sender identity
        // ARN. The verified identity must be set up out-of-band (AWS
        // console or a separate construct); this grant says "allowed
        // to send AS this sender," not "allowed to verify identities."
        //
        // IMPORTANT: this construct assumes an EMAIL identity, not a
        // domain identity. The EMAIL_PATTERN above rejects bare-domain
        // input precisely so the ARN here scopes to a single address,
        // not to every address in a domain. If you ever switch to a
        // domain identity (and intentionally want a domain-wide grant),
        // this construct is the wrong shape — author a separate one
        // that documents the wider blast radius explicitly.
        const region = Stack.of(this).region;
        const account = Stack.of(this).account;
        const senderIdentityArn = `arn:aws:ses:${region}:${account}:identity/${senderEmail}`;
        notifierFn.addToRolePolicy(new iam.PolicyStatement({
            actions: ['ses:SendEmail'],
            resources: [senderIdentityArn],
        }));

        this.function = notifierFn;

        new CfnOutput(this, 'NotifierFunctionName', {
            value: notifierFn.functionName,
            description: 'Lambda function name for the alert notifier',
        });
        new CfnOutput(this, 'NotifierSenderEmail', {
            value: senderEmail,
            description: 'Verified SES sender identity used by the notifier',
        });
    }
}

module.exports = { NotifierServerConstruct };
