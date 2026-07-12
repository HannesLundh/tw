"""FastAPI web interface for the chat agent."""

from pathlib import Path
import sys

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

# Add repo root to path for imports
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

# Import from the chat module (same directory)
from chat.chat import answer_turn, load_config, create_client  # noqa: E402

app = FastAPI()

# Load configuration and create client
config = load_config()
client = create_client(config)

# Serve static files from chat/static
static_dir = Path(__file__).parent / "static"
if not static_dir.exists():
    raise RuntimeError(f"Static directory not found: {static_dir}")

app.mount("/static", StaticFiles(directory=static_dir), name="static")

@app.get("/", response_class=HTMLResponse)
async def get_index():
    """Serve the main chat interface HTML."""
    html_path = static_dir / "index.html"
    if not html_path.exists():
        raise HTTPException(
            status_code=500,
            detail=f"Chat interface file not found: {html_path}"
        )
    try:
        return HTMLResponse(html_path.read_text())
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error reading chat interface: {str(e)}"
        )

@app.post("/api/chat")
async def chat_endpoint(request: Request):
    """Handle chat requests."""
    try:
        data = await request.json()
        messages = data.get("messages", [])
        
        # Add system prompt if not already present
        system_prompt_path = REPO_ROOT / config["system_prompt"]
        system_prompt = system_prompt_path.read_text()
        if not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system_prompt})
        
        reply = answer_turn(client, config, messages)
        return {"reply": reply}
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Error processing chat request: {str(e)}"
        )
