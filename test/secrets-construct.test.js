/**
 * Group A — SecretsConstruct (ADR 0006).
 *
 * Isolated synth (no Docker, no full stack): instantiate the construct
 * on a bare Stack and assert the two Secrets Manager resources, their
 * names, the generateSecretString config, and the exposed Secret
 * instances.
 */
const { App, Stack } = require('aws-cdk-lib');
const { Template } = require('aws-cdk-lib/assertions');
const secretsmanager = require('aws-cdk-lib/aws-secretsmanager');
const SecretsConstruct = require('../lib/secrets');

function build() {
    const app = new App();
    const stack = new Stack(app, 'SecretsTestStack');
    const construct = new SecretsConstruct(stack, 'SecretsConstruct');
    const template = Template.fromStack(stack);
    return { construct, template };
}

describe('SecretsConstruct', () => {
    test('A1 synthesises exactly two AWS::SecretsManager::Secret resources', () => {
        const { template } = build();
        template.resourceCountIs('AWS::SecretsManager::Secret', 2);
    });

    test('A2 agent secret name is trip-tracker-agent-jwt-signer', () => {
        const { template } = build();
        template.hasResourceProperties('AWS::SecretsManager::Secret', {
            Name: 'trip-tracker-agent-jwt-signer',
        });
    });

    test('A3 poller secret name is trip-tracker-poller-jwt-signer', () => {
        const { template } = build();
        template.hasResourceProperties('AWS::SecretsManager::Secret', {
            Name: 'trip-tracker-poller-jwt-signer',
        });
    });

    test('A4 generateSecretString pins length 40 + excludePunctuation', () => {
        const { template } = build();
        const secrets = template.findResources('AWS::SecretsManager::Secret');
        const configs = Object.values(secrets).map(
            (r) => r.Properties.GenerateSecretString,
        );
        expect(configs).toHaveLength(2);
        for (const cfg of configs) {
            expect(cfg.PasswordLength).toBe(40);
            expect(cfg.ExcludePunctuation).toBe(true);
        }
    });

    test('A5 exposes agentJwtSecret + pollerJwtSecret as Secret instances', () => {
        const { construct } = build();
        expect(construct.agentJwtSecret).toBeInstanceOf(secretsmanager.Secret);
        expect(construct.pollerJwtSecret).toBeInstanceOf(secretsmanager.Secret);
        expect(construct.agentJwtSecret.secretArn).toBeDefined();
        expect(construct.pollerJwtSecret.secretArn).toBeDefined();
    });
});
