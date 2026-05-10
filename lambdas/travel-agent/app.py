"""
Lambda entrypoint for the travel agent.

Design considerations:
- Keep this file thin. It is the HTTP/Lambda boundary, not the place for agent
  orchestration logic. Its job is to authenticate the caller, extract request
  context, and delegate business logic to agent.py.
- Re-validate the JWT here even though API Gateway already uses a custom
  authorizer. That is defense in depth: the function does not blindly trust an
  upstream integration and can still reject requests if it is ever invoked
  through another path or the gateway configuration changes.
- Include user and network context in the composite prompt intentionally. This
  gives the downstream agent enough context to personalise responses and apply
  policy-sensitive logic without coupling the prompt-building step to the agent
  implementation itself.
- Keep the Lambda stateless. Conversation memory lives behind agent.py via an
  external session manager, which makes this handler safe for retries,
  concurrency, and cold starts.
"""

import logger
import agent
import json
import jwt
import os
from user import User

l = logger.get()

# Used when the agent later calls MCP servers that require internally signed JWTs.
# It is loaded here because the Lambda environment is the root configuration
# boundary, even though this file itself does not sign the outgoing MCP tokens.
JWT_SIGNATURE_SECRET = os.environ['JWT_SIGNATURE_SECRET']

# Cognito publishes public signing keys at this URL. We use them to verify that
# the incoming bearer token was really issued by Cognito and was not tampered with.
COGNITO_JWKS_URL = os.environ['COGNITO_JWKS_URL']

# PyJWKClient caches and resolves the correct public key from the JWT header
# (`kid`) so token verification can happen without hard-coding keys.
jwks_client = jwt.PyJWKClient(COGNITO_JWKS_URL)

def get_jwt_claims(authorization_header):
    # API Gateway forwards the Authorization header as "Bearer <token>".
    jwt_string = authorization_header.split(" ")[1]
    # print(jwt_string)

    # Resolve the matching Cognito public key for this token, then validate the
    # signature before trusting any claim in the payload.
    signing_key = jwks_client.get_signing_key_from_jwt(jwt_string)
    claims = jwt.decode(jwt_string, signing_key.key, algorithms=["RS256"])
    # print(claims)
    return claims

def handler(event: dict, ctx):
    l.info("> handler")
    try:
        # Parse and verify the caller's JWT, then reduce it to the minimal user
        # identity the rest of the agent stack actually needs.
        claims = get_jwt_claims(event["headers"]["Authorization"])
        user = User(id=claims["sub"], name=claims["username"])
        l.info(f"jwt parsed. user.id={user.id} user.name={user.name}")
    except Exception as e:
        # Reject early if authentication fails. This avoids spending LLM/tooling
        # cost on requests that are malformed or unauthorised.
        l.error("failed to parse jwt: ", exc_info=True)
        return {
            "statusCode": 401,
            "body": 'Unauthorized'
        }

    # Capture request metadata separately from the prompt text. The source IP is
    # included in the prompt as contextual signal; policy or auditing logic can
    # use it without needing direct access to the raw API Gateway event later.
    source_ip = event["requestContext"]["identity"]["sourceIp"]
    request_body: dict = json.loads(event["body"])
    prompt_text = request_body["text"]

    # Build a single composite prompt at the boundary. This keeps the agent API
    # simple (one string in, one string out) while still giving the model enough
    # structured context about the user and request.
    composite_prompt = f"User name: {user.name}\n"
    composite_prompt += f"User IP: {source_ip}\n"
    composite_prompt += f"User prompt: {prompt_text}"
    l.info(f"composite_prompt={composite_prompt}")
    
    # All session management, MCP tool discovery, and model orchestration live
    # in agent.py. Keeping that complexity out of the handler makes the Lambda
    # easier to reason about and test at the transport boundary.
    response_text = agent.prompt(user, composite_prompt)
    l.info(f"response_text={response_text}")
    
    # Return a minimal JSON payload for the web app. The UI only needs the final
    # text response; internal reasoning and tool details stay in logs.
    return {
        "statusCode": 200,
        "body": json.dumps({"text": response_text})
    }


if __name__ == "__main__":
    # Lightweight local harness for manual testing. It simulates the shape of an
    # API Gateway event so the handler can be exercised without deploying first.
    debug_token = "your-debug-token"

    l.info("in __main__, you're probably testing, right?")
    body = json.dumps({
        "text": "Book me a trip to New York"
    })
    event = {
        "requestContext": {
            "identity": {
                "sourceIp": "70.200.50.45"
            }
        },
        "headers": {
            "Authorization": f"Bearer {debug_token}"
        },
        "body": body
    }

    l.info('round 1')
    handler_response1 = handler(event, None)
    l.info(f"handler_response1: {handler_response1}")

    # print('round 2')
    # handler_response2 = handler(event, None)
    # l.info(f"handler_response2: {handler_response2}")
