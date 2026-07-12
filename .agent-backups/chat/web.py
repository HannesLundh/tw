"""FastAPI web interface for the chat agent."""

from pathlib import Path
import sys

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))

from chat.chat import answer_turn, load_config, create_client  # noqa: E402

app = FastAPI()

# Load configuration and create client
config = load_config()
client = create_client(config)

# Serve static files from chat/static
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serve the main chat interface HTML."""
    html_path = Path(__file__).parent / "static" / "index.html"
    if not html_path.exists():
        return HTMLResponse("<h1>Chat Interface</h1><p>index.html not found</p>")
    return HTMLResponse(html_path.read_text())


@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Handle chat requests."""
    data = await request.json()
    messages = data.get("messages", [])
    
    # Add system prompt if not already present
    system_prompt_path = REPO_ROOT / config["system_prompt"]
    system_prompt = system_prompt_path.read_text()
    if not any(m.get("role") == "system" for m in messages):
        messages.insert(0, {"role": "system", "content": system_prompt})
    
    try:
        reply = answer_turn(client, config, messages)
        return {"reply": reply}
    except Exception as e:
        return {"reply": f"Error: {str(e)}"}
