#!/bin/sh
# Post-deploy helper: set the demo users' passwords and write web/.env from
# the deployed stack's CloudFormation outputs.
#
# The stack's CfnOutputs do NOT set ExportName, so they must be looked up by
# OutputKey (a generated name like "CognitoCognitoUserPoolId0B9F70D4"), not
# by export. We match on a stable OutputKey *substring* and take the first
# hit, which is unique for each value below.
set -e
DST_FILE_NAME="./web/.env"
STACK_NAME="TripTrackerStack"

# get_output <OutputKey substring> — prints the matching output value.
get_output() {
    aws cloudformation describe-stacks \
        --stack-name "$STACK_NAME" \
        --query "Stacks[0].Outputs[?contains(OutputKey, '$1')]|[0].OutputValue" \
        --output text
}

echo "STACK_NAME=$STACK_NAME"

echo "> Setting passwords for Alice and Bob"
COGNITO_USER_POOL_ID=$(get_output UserPoolId)
if [ -z "$COGNITO_USER_POOL_ID" ] || [ "$COGNITO_USER_POOL_ID" = "None" ]; then
    echo "ERROR: could not resolve the Cognito user pool id from stack" \
         "'$STACK_NAME'. Is it deployed in this account/region?" >&2
    exit 1
fi
echo "COGNITO_USER_POOL_ID=\"$COGNITO_USER_POOL_ID\""
aws cognito-idp admin-set-user-password --user-pool-id "$COGNITO_USER_POOL_ID" --username Alice --password "Passw0rd@" --permanent
aws cognito-idp admin-set-user-password --user-pool-id "$COGNITO_USER_POOL_ID" --username Bob   --password "Passw0rd@" --permanent

echo "> Writing $DST_FILE_NAME"
{
    echo "COGNITO_SIGNIN_URL=\"$(get_output SignInUrl)\""
    echo "COGNITO_LOGOUT_URL=\"$(get_output LogoutUrl)\""
    echo "COGNITO_WELL_KNOWN_URL=\"$(get_output WellKnownUrl)\""
    echo "COGNITO_CLIENT_ID=\"$(get_output ClientId)\""
    echo "COGNITO_CLIENT_SECRET=\"$(get_output ClientSecret)\""
    echo "AGENT_ENDPOINT_URL=\"$(get_output AgentEndpointUrl)\""
} > "$DST_FILE_NAME"

cat "$DST_FILE_NAME"
