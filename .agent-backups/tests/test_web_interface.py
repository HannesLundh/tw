"""Tests for the FastAPI web interface."""

import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient

# Import the app from chat.web
from chat.web import app

client = TestClient(app)


def test_get_index_returns_html():
    """Test that GET / returns HTML content."""
    response = client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    # Check for key elements in the HTML
    html_content = response.text
    assert "Local Chat Agent" in html_content
    assert "id="\"user-input\"" in html_content
    assert "id="\"send-button\"" in html_content
    assert "Thinking..." in html_content


def test_post_chat_endpoint():
    """Test that POST /api/chat accepts messages and returns reply."""
    # Test with minimal messages
    test_messages = [
        {"role": "user", "content": "Hello"}
    ]
    
    response = client.post(
        "/api/chat",
        json={"messages": test_messages}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data
    # The reply should be a string (even if empty or error message)
    assert isinstance(data["reply"], str)


def test_post_chat_with_empty_messages():
    """Test POST /api/chat with empty messages list."""
    response = client.post(
        "/api/chat",
        json={"messages": []}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data


def test_post_chat_with_invalid_json():
    """Test POST /api/chat with invalid JSON."""
    response = client.post(
        "/api/chat",
        json={"invalid": "data"}
    )
    
    # Should still return 200 but reply might be an error message
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data


def test_static_files_served():
    """Test that static files are served correctly."""
    # Check if index.html exists and is accessible
    static_path = Path("chat/static/index.html")
    assert static_path.exists(), "index.html should exist in chat/static/"
    
    response = client.get("/static/index.html")
    assert response.status_code == 200
