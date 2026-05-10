const { Construct } = require('constructs');
const cognito = require('aws-cdk-lib/aws-cognito');
const { CfnOutput, RemovalPolicy, Stack, Fn, Names, Duration } = require('aws-cdk-lib');

const REDIRECT_URI = "http://localhost:8000/callback";
const LOGOUT_URI = "http://localhost:8000/chat";

class Cognito extends Construct {
    constructor(scope, id, props) {
        super(scope, id, props);

        const userPool = new cognito.UserPool(this, 'UserPool', {
            selfSignUpEnabled: false,
            signInAliases: { username: true, email: true },
            removalPolicy: RemovalPolicy.DESTROY
        });

        const userPoolClient = new cognito.UserPoolClient(this, 'UserPoolClient', {
            userPool,
            generateSecret: true,
            authFlows: {
                userPassword: true
            },
            oAuth: {
                flows: {
                    authorizationCodeGrant: true
                },
                callbackUrls: [REDIRECT_URI],
                logoutUrls: [LOGOUT_URI]
            },
            accessTokenValidity: Duration.hours(8),
            idTokenValidity: Duration.hours(8)
        });

        const userPoolRandomId = Names.uniqueId(userPool).slice(-8).toLowerCase();
        const userPoolDomain = userPool.addDomain('UserPoolDomain', {
            cognitoDomain: {
                domainPrefix: `strands-on-lambda-${userPoolRandomId}`
            }
        });

        new cognito.CfnUserPoolUser(this, 'AliceUser', {
            userPoolId: userPool.userPoolId,
            messageAction: 'SUPPRESS',
            username: 'Alice',
        });

        new cognito.CfnUserPoolUser(this, 'BobUser', {
            userPoolId: userPool.userPoolId,
            messageAction: 'SUPPRESS',
            username: 'Bob',
        });

        // Outputs
        const region = Stack.of(this).region;
        const cognitoJwksUrl = `https://cognito-idp.${region}.amazonaws.com/${userPool.userPoolId}/.well-known/jwks.json`;
        const cognitoWellKnownUrl = `https://cognito-idp.${region}.amazonaws.com/${userPool.userPoolId}/.well-known/openid-configuration`;
        const cognitoSignInUrl = userPoolDomain.signInUrl(userPoolClient, {
            redirectUri: REDIRECT_URI
        });
        const cognitoLogoutUrl = `${userPoolDomain.baseUrl()}/logout?client_id=${userPoolClient.userPoolClientId}`;

        new CfnOutput(this, 'CognitoUserPoolId', {
            value: userPool.userPoolId
        });

        new CfnOutput(this, 'CognitoWellKnownUrl', {
            value: cognitoWellKnownUrl
        });

        new CfnOutput(this, 'CognitoSignInUrl', {
            value: cognitoSignInUrl
        });

        new CfnOutput(this, 'CognitoLogoutUrl', {
            value: cognitoLogoutUrl
        });

        new CfnOutput(this, 'CognitoClientId', {
            value: userPoolClient.userPoolClientId
        });

        new CfnOutput(this, 'CognitoClientSecret', {
            // unsafeUnwrap() is used here for brevity and simplicity only.
            // Always use Secrets Manager to store your secrets!!!
            value: userPoolClient.userPoolClientSecret.unsafeUnwrap()
        });

        new CfnOutput(this, 'CognitoJwksUrl', {
            value: cognitoJwksUrl
        });

        return {
            cognitoJwksUrl
        }
    }
}

module.exports = Cognito;