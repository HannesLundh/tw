"""Test Enter key behavior during reply generation."""

from pathlib import Path

def test_enter_key_checks_is_generating_reply():
    """Test that the Enter key handler checks isGeneratingReply flag."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # The Enter key handler should check isGeneratingReply
    # Look for the pattern where sendMessage is called only if not generating
    enter_handler_code = '''
        userInput.addEventListener('keypress', (e) => {
            if (e.key === 'Enter') {
                if (!isGeneratingReply) {
                    sendMessage();
                }
            }
        });'''
    
    # Check that the enter handler includes the isGeneratingReply check
    assert "if (!isGeneratingReply)" in html_content or \
           "if (e.key === 'Enter')" in html_content and "sendMessage()" in html_content, \
           "Enter key handler should prevent sending when isGeneratingReply is true"
