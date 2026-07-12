"""Test that Enter key is disabled during reply generation."""

from pathlib import Path

def test_is_generating_reply_flag_exists():
    """Test that the isGeneratingReply flag is present in the HTML."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check for the flag declaration
    assert "let isGeneratingReply = false;" in html_content, \
        "isGeneratingReply flag should be declared"
    
    # Check that it's used to prevent sending during generation
    assert "if (isGeneratingReply)" in html_content, \
        "isGeneratingReply should be checked before sending"
    
    # Check that it's set to true when starting generation
    assert "isGeneratingReply = true;" in html_content, \
        "isGeneratingReply should be set to true when generating"
    
    # Check that it's reset to false when done
    assert "isGeneratingReply = false;" in html_content, \
        "isGeneratingReply should be reset to false after generation"


def test_send_button_disabled_during_generation():
    """Test that send button is disabled during reply generation."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check that the input event handler includes isGeneratingReply
    assert "sendButton.disabled = userInput.value.trim() === '' || isGeneratingReply;" in html_content, \
        "Send button should be disabled when isGeneratingReply is true"


def test_enter_key_handler_exists():
    """Test that Enter key handler still exists."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check for the Enter key handler
    assert "userInput.addEventListener('keypress', (e) => {" in html_content, \
        "Enter key handler should exist"
    assert "if (e.key === 'Enter')" in html_content, \
        "Enter key check should exist"
