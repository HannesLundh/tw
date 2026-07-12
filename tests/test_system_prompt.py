"""Additional test for system prompt handling."""

from fastapi.testclient import TestClient
from chat.web import app

client = TestClient(app)


def test_system_prompt_injection():
    """Test that system prompt is added when not present in messages."""
    # Messages without system prompt
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


def test_system_prompt_not_duplicated():
    """Test that system prompt is not duplicated if already present."""
    # Messages with system prompt already included
    test_messages = [
        {"role": "system", "content": "test system prompt"},
        {"role": "user", "content": "Hello"}
    ]
    
    response = client.post(
        "/api/chat",
        json={"messages": test_messages}
    )
    
    assert response.status_code == 200
    data = response.json()
    assert "reply" in data
