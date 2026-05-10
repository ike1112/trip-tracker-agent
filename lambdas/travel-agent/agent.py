"""
Core orchestration layer for the travel agent.

Design considerations:
- Keep orchestration separate from the Lambda transport boundary. `app.py`
    should only deal with HTTP/Lambda concerns, while this module owns agent
    setup, memory, and tool wiring.
- Persist conversation state outside the Lambda runtime. Lambda execution
    environments are ephemeral, so multi-turn chat history must live in S3 rather
    than in local memory.
- Build the agent per request, but anchor it to a stable session ID per user.
    That gives each user durable conversation continuity without sharing state
    across users.
- Combine local tools and MCP-discovered remote tools into one tool list. This
    keeps the LLM-facing interface simple: the model sees one toolbox, regardless
    of whether a tool is implemented in-process or on a remote MCP server.
"""

from strands import Agent
from strands.session.s3_session_manager import S3SessionManager
import os
from aws_lambda_powertools import Logger
from user import User
import mcp_client_manager
import tools
from watches import make_watch_tools
from agent_config import model, system_prompt

l = Logger(service="travel-agent")

# Bucket name is injected by CDK so the same code can run in different
# environments without hard-coding infrastructure identifiers.
SESSION_STORE_BUCKET_NAME = os.environ['SESSION_STORE_BUCKET_NAME']
l.info(f"SESSION_STORE_BUCKET_NAME={SESSION_STORE_BUCKET_NAME}")

def prompt(user: User, composite_prompt: str):
    """
    Execute one turn of the conversation for a specific authenticated user.

    Inputs:
    - `user`: minimal identity object derived from the verified JWT
    - `composite_prompt`: the request text plus boundary context assembled in app.py

    Returns the final assistant text to send back to the web client.
    """
    l.info(f"user.id={user.id}, user.name={user.name}")

    # Use a stable per-user session ID so the Strands session manager can load
    # and append prior conversation state from S3. This is what makes the agent
    # feel conversational across multiple requests instead of stateless.
    session_manager = S3SessionManager(
        session_id=f"session_for_user_{user.id}",
        bucket=SESSION_STORE_BUCKET_NAME,
        prefix="agent_sessions"
    )

    try:
        # Resolve MCP tools for this user. The MCP client manager is responsible
        # for creating user-scoped authenticated tool connections and reusing
        # them when possible.
        mcp_tools = mcp_client_manager.get_mcp_tools_for_user(user)

        # Build watch CRUD tools as closures bound to this user's verified id.
        # The LLM never sees user.id in the tool schema — see ADR 0001.
        watch_tools = make_watch_tools(user.id)

        # Construct the Strands agent on demand so each invocation uses the
        # current config, session manager, and resolved tool set.
        agent = Agent(
            model=model,
            # agent_id is a logical name used by Strands for tracing/state.
            agent_id="travel_agent",
            session_manager=session_manager,
            system_prompt=system_prompt,
            callback_handler=None,
            # One toolbox: location/date helpers (module), watch CRUD (user-bound
            # closures), and MCP-backed remote tools.
            tools=[tools] + watch_tools + mcp_tools,
        )

        # Invoke the agent with the fully prepared prompt. Strands handles the
        # underlying model call, session state updates, and tool-use loop.
        agent_response = agent(composite_prompt)

        # The SDK returns a richer structured message object; the web UI only
        # needs the final text content for this demo application.
        response_text = agent_response.message["content"][0]["text"]
        return response_text

    except Exception as e:
        # Collapse internal setup/tooling failures into a generic user-facing
        # message while preserving the full stack trace in logs for debugging.
        l.exception(e)
        return 'Failed to initialize MCP Client, see logs'

