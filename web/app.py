# Standard library and third-party imports
import os
from starlette.middleware.sessions import SessionMiddleware  # Adds server-side session support to the app
from fastapi import FastAPI, Request, HTTPException           # Core FastAPI components
import dotenv       # Loads environment variables from a .env file
import uvicorn      # ASGI server used to run the FastAPI app
import gradio as gr # UI framework for building the chat interface
import httpx        # Async-capable HTTP client for calling the agent endpoint
import oauth        # Local module that handles OAuth2 / Cognito login flows

# Load environment variables from .env (if present); values can be overridden by the shell environment
dotenv.load_dotenv()

# The URL of the deployed Lambda-backed travel agent endpoint, injected at runtime
AGENT_ENDPOINT_URL = os.getenv("AGENT_ENDPOINT_URL")
print(f"AGENT_ENDPOINT_URL={AGENT_ENDPOINT_URL}")

# Avatar image URLs shown next to each message in the chat UI
user_avatar = "https://cdn-icons-png.flaticon.com/512/149/149071.png"
bot_avatar  = "https://cdn-icons-png.flaticon.com/512/4712/4712042.png"

# Create the FastAPI application instance that handles all routing and requests
fastapi_app = FastAPI()

# Attach session middleware so the app can persist login state across requests.
# After a successful login, data like access_token and username are stored in a
# signed cookie on the user's browser. "Signed" means the cookie is
# cryptographically tied to secret_key, preventing the user from tampering with
# its contents. On every subsequent request, the middleware reads that cookie and
# makes its contents available via req.session (e.g. req.session["access_token"]).
fastapi_app.add_middleware(SessionMiddleware, secret_key="secret")

# Register the OAuth2 routes (/login, /callback, /logout) defined in oauth.py
oauth.add_oauth_routes(fastapi_app)


def check_auth(req: Request):
    """
    Dependency used by Gradio's auth_dependency parameter.
    Verifies that a valid access token and username exist in the session.
    If either is missing the user is redirected to the login page.
    Returns the username string on success, which Gradio exposes as request.username.
    """
    if not "access_token" in req.session or not "username" in req.session:
        print("check_auth::not found, redirecting to /login")
        # HTTP 302 redirect is achieved by raising an HTTPException with a Location header
        raise HTTPException(status_code=302, detail="Redirecting to login", headers={"Location": "/login"})

    username = req.session["username"]
    print(f"check_auth::auth found username: {username}")
    return username


def chat(message, history, request: gr.Request):
    """
    Gradio chat handler called every time the user sends a message.
    - Retrieves the authenticated username and bearer token from the session.
    - Forwards the user's message to the travel-agent Lambda endpoint.
    - Returns the agent's text reply, or a descriptive error string on failure.
    """
    username = request.username
    # Retrieve the Cognito access token stored in the session during OAuth callback
    token = request.request.session["access_token"]
    print(f"username={username}, message={message}")
    print(f"token={token}")

    # POST the user message to the agent; the bearer token authorises the request
    agent_response = httpx.post(
        AGENT_ENDPOINT_URL,
        headers={"Authorization": f"Bearer {token}"},
        json={"text": message},
        timeout=30,  # seconds – Lambda cold-start can add latency
    )

    # Surface auth errors clearly so the user knows to re-login
    if agent_response.status_code == 401 or agent_response.status_code == 403:
        return f"Agent returned authorization error. Try to re-login. Status code: {agent_response.status_code}"

    # Any other non-200 response is treated as a generic failure
    if agent_response.status_code != 200:
        return f"Failed to communicate with Agent. Status code: {agent_response.status_code}"

    # Extract the agent's reply from the JSON response body
    response_text = agent_response.json()['text']
    return response_text


def on_gradio_app_load(request: gr.Request):
    """
    Called once when the Gradio page first loads for an authenticated user.
    Returns two values that are mapped to the logout button label and the
    initial chat message via gradio_app.load() below.
    """
    return f"Logout ({request.username})", [gr.ChatMessage(
        role="assistant",
        content=(
            f"Hi {request.username}, I track trip prices for you. "
            "Describe a trip — origin, destination, dates, nights, budget — "
            "and I'll watch the combined flight + hotel cost and alert you "
            "when it's worth booking. What trip should I watch? "
        )
    )]


# ---------------------------------------------------------------------------
# Gradio UI definition
# ---------------------------------------------------------------------------
with gr.Blocks() as gradio_app:
    # Page title shown above the chat interface
    header = gr.Markdown("Trip Tracker — combined flight + hotel price watch")

    # Collapsible section that shows the system architecture diagram
    with gr.Accordion("Architecture (click to open)", open=False):
        gr.Image(value='arch.png', show_label=False)

    # ChatInterface wires the chat() function to the Gradio chatbot component
    chat_interface = gr.ChatInterface(
        fn=chat,
        type="messages",
        chatbot=gr.Chatbot(
            type="messages",
            label="Track a trip's flight + hotel price over time",
            avatar_images=(user_avatar, bot_avatar),
            placeholder="<b>Trip Tracker</b> — describe a trip and I'll watch its price."
        )
    )

    # Logout button uses a small JS snippet to navigate to the /logout route
    logout_button = gr.Button(value="Logout", variant="secondary")
    logout_button.click(
        fn=None,
        js="() => window.location.href='/logout'"
    )

    # When the page loads, populate the logout button label and post the greeting message
    gradio_app.load(on_gradio_app_load, inputs=None, outputs=[logout_button, chat_interface.chatbot])

# Mount the Gradio app under /chat; check_auth guards every request to this path
gr.mount_gradio_app(fastapi_app, gradio_app, path="/chat", auth_dependency=check_auth)

if __name__ == "__main__":
    # Start the ASGI server, listening on all interfaces at port 8000.
    # This is the entry point when running the container locally or in Lambda Web Adapter mode.
    uvicorn.run(fastapi_app, host="0.0.0.0", port=8000)

