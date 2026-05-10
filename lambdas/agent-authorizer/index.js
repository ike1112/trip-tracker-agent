// Lambda TOKEN Authorizer Overview
// --------------------------------
// This function protects the travel-agent API by validating Cognito JWTs before
// API Gateway forwards requests to the main agent Lambda.
//
// Request flow:
// 1) API Gateway receives "Authorization: Bearer <token>".
// 2) It invokes this authorizer and passes the token in `event.authorizationToken`.
// 3) We read the JWT header `kid` and fetch the matching public key from Cognito JWKS.
// 4) We verify the token signature using RS256 and trust claims only if verification passes.
// 5) On success, we return an IAM policy with Effect=Allow for the requested method ARN.
// 6) On any failure (missing token, parse error, bad signature, key fetch issue),
//    we return Effect=Deny (fail-closed) so unauthenticated requests are blocked.

import jwt from 'jsonwebtoken';
import jwksClient from 'jwks-rsa';
import { promisify } from 'util';

// Cognito publishes rotating public keys at this JWKS( JSON Web Key Set) endpoint.
// We use these keys to verify JWT signatures without storing any secret in this Lambda.
const COGNITO_JWKS_URL = process.env.COGNITO_JWKS_URL;

// JWKS client caches keys to avoid fetching from Cognito on every request,
// reducing latency and protecting the authorizer from network spikes.
const client = jwksClient({
    jwksUri: COGNITO_JWKS_URL,
    cache: true
});

// jwt.verify is callback-based; promisify lets us use async/await for clearer control flow.
const verifyJwt = promisify(jwt.verify);

// jsonwebtoken calls this function with the token header.
// We use the header's `kid` to fetch the matching Cognito public key that signed the token.
function getKey(header, callback) {
    // console.log(`>getKey`);
    client.getSigningKey(header.kid, (err, key) => {
        if (err) return callback(err);
        const signingKey = key.getPublicKey();
        callback(null, signingKey);
    });
}

export const handler = async (event) => {
    // API Gateway TOKEN authorizer passes "Bearer <jwt>" in authorizationToken.
    const authHeader = event.authorizationToken;
    // console.log({authHeader});
    try {
        // Extract the raw JWT from the Authorization header.
        const jwtString = authHeader.split(' ')[1];

        // Verify signature and algorithm before trusting any claim.
        // RS256 ensures we only accept asymmetric signatures from Cognito-issued tokens.
        const claims = await verifyJwt(jwtString, getKey, { algorithms: ['RS256'] });
        // console.log({ claims });

        // Include stable identity info in principalId so downstream integrations
        // can identify who was authorized for this request.
        const principalId = `${claims.sub}|${claims.username}`;
        return generatePolicy('Allow', event.methodArn, principalId);
    } catch (e) {
        // Any parse, signature, or key lookup failure results in a deny policy.
        // Fail-closed behavior prevents unauthenticated access.
        console.error(`Failed to parse authorization header: ${e}`);
        return generatePolicy('Deny', event.methodArn);
    }

};

const generatePolicy = (effect, resource, principalId) => {
    console.log(`generatePolicy effect=${effect} pricipalId=${principalId}`);

    // API Gateway expects an IAM policy document as the authorizer response.
    // The Effect controls whether the current request is allowed to invoke this API method.
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