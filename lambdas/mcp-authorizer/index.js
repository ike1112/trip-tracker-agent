import jwt from 'jsonwebtoken';
import { SecretsManagerClient, GetSecretValueCommand } from '@aws-sdk/client-secrets-manager';

/*
 * ADR 0006 two-secret + sub-coupling JWT verifier.
 *
 * This block is COPIED VERBATIM into lambdas/mcp-authorizer/index.js,
 * lambdas/flights-mcp/index.js and lambdas/hotels-mcp/index.js. It is
 * intentionally triplicated, not a shared module: a cross-Lambda-package
 * shared verifier would need a Lambda layer or a monorepo symlink, both
 * larger than this hardening change and a new failure surface. Each
 * package's tests pin the identical cross-sub-forgery + foreign-secret
 * invariant. EDIT ALL THREE COPIES TOGETHER.
 */
let secretsClient = new SecretsManagerClient();
const _secretCache = {};

// Lazy: fetch on first verify, not at module load, so tests can seed
// _secretCache before any AWS SDK call fires (ADR 0006).
async function getSecret(envVar) {
    const arn = process.env[envVar];
    if (!arn) throw new Error(`${envVar} env var is required`);
    if (_secretCache[arn] == null) {
        const out = await secretsClient.send(new GetSecretValueCommand({ SecretId: arn }));
        _secretCache[arn] = out.SecretString;
    }
    return _secretCache[arn];
}

// Each signing secret may mint exactly one sub. A token must verify
// under a secret AND carry that secret's allowed sub — without the
// coupling a leaked agent token would also pass as a poller token.
const SECRET_SUBS = [
    ['AGENT_JWT_SECRET_ARN',  'travel-agent'],
    ['POLLER_JWT_SECRET_ARN', 'trip-tracker-poller'],
];

async function verifyTwoSecret(token) {
    for (const [envVar, allowedSub] of SECRET_SUBS) {
        // Fetch OUTSIDE the verify try: a Secrets Manager / KMS failure
        // is an infra error, not a bad signature. It still fails closed
        // (the handler's catch denies), but it surfaces with its own
        // error message instead of being laundered into the generic
        // "no candidate secret verified" deny — so the two alarm apart.
        const secret = await getSecret(envVar);
        let claims;
        try {
            // Pin HS256 explicitly — don't rely on the library default
            // to block alg=none / RS-HS confusion (defense in depth).
            claims = jwt.verify(token, secret, { algorithms: ['HS256'] });
        } catch {
            continue; // wrong secret / expired / malformed — try the next
        }
        // Enforce expiry at the boundary: a token minted without exp
        // must not be treated as eternal just because a minter slipped.
        if (claims.exp === undefined) continue;
        if (claims.sub === allowedSub) return claims;
        // Verified under this secret but wrong sub: cross-sub forgery.
        // Do not echo the attacker-influenced sub into the log line.
        throw new Error('sub not allowed for this secret');
    }
    throw new Error('no candidate secret verified the token');
}

// Test seam: seed the cache so getSecret never calls AWS in unit tests.
export function __seedSecretCacheForTests(map) {
    for (const k of Object.keys(_secretCache)) delete _secretCache[k];
    for (const [arn, value] of Object.entries(map)) _secretCache[arn] = value;
}
/* end ADR 0006 verifier block */

export const handler = async (event) => {
    const authHeader = event.authorizationToken;
    try {
        const jwtString = authHeader.split(' ')[1];
        const claims = await verifyTwoSecret(jwtString);
        const principalId = `${claims.sub}|${claims.user_id}|${claims.user_name}`;
        return generatePolicy('Allow', event.methodArn, principalId);
    } catch (e) {
        console.error(`authorizer deny: ${e.message}`);
        return generatePolicy('Deny', event.methodArn);
    }
};

const generatePolicy = (effect, resource, principalId) => {
    console.log(`generatePolicy effect=${effect} principalId=${principalId}`);
    return {
        principalId,
        policyDocument: {
            Version: '2012-10-17',
            Statement: [{
                Action: 'execute-api:Invoke',
                Effect: effect,
                Resource: resource
            }]
        }
    };
};
