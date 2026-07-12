"""Test Enter key behavior during reply generation."""

from pathlib import Path

def test_enter_key_event_handler_exists():
    """Test that the Enter key event handler exists and calls sendMessage."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check for the Enter key handler
    assert "userInput.addEventListener('keypress', (e) => {" in html_content, \
        "Enter key event listener should exist"
    
    # Check that it calls sendMessage when Enter is pressed
    assert "if (e.key === 'Enter')" in html_content, \
        "Enter key check should exist"
    
    # The handler should call sendMessage
    enter_handler_section = html_content.split("userInput.addEventListener('keypress'")[1].split("});")[0]
    assert "sendMessage()" in enter_handler_section, \
        "Enter key handler should call sendMessage()"

def test_send_message_checks_is_generating_reply():
    """Test that sendMessage function checks isGeneratingReply flag."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check that sendMessage has the guard clause
    assert "if (isGeneratingReply) {" in html_content, \
        "sendMessage should check isGeneratingReply"
    
    # Find the sendMessage function and verify it returns early
    send_message_start = html_content.find("async function sendMessage() {")
    send_message_end = html_content.find("}", send_message_start)
    send_message_code = html_content[send_message_start:send_message_end + 1]
    
    # The first check should be for isGeneratingReply
    assert "if (isGeneratingReply) {" in send_message_code, \
        "sendMessage should have isGeneratingReply guard clause"
    
    # And it should return immediately after that check
    is_generating_check = send_message_code.find("if (isGeneratingReply) {")
    next_lines = send_message_code[is_generating_check:is_generating_check + 100]
    assert "return;" in next_lines, \
        "sendMessage should return immediately if isGeneratingReply is true"
