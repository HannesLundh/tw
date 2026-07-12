"""Test CSS styling for message bubbles."""

from pathlib import Path

def test_message_bubbles_have_pre_wrap():
    """Test that message bubbles use white-space: pre-wrap to preserve newlines."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check that the .message class has white-space: pre-wrap
    assert "white-space: pre-wrap;" in html_content, \
        "Message bubbles should use white-space: pre-wrap to preserve newlines"
    
    # Find the .message CSS rule and verify it contains pre-wrap
    message_rule_start = html_content.find(".message {")
    message_rule_end = html_content.find("}", message_rule_start)
    message_css = html_content[message_rule_start:message_rule_end + 1]
    
    assert "white-space: pre-wrap;" in message_css, \
        ".message CSS rule should include white-space: pre-wrap"
