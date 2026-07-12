"""Test thinking indicator positioning."""

from pathlib import Path

def test_thinking_indicator_inside_input_area():
    """Test that the thinking indicator is positioned inside the input area."""
    html_path = Path("chat/static/index.html")
    html_content = html_path.read_text()
    
    # Check that the thinking indicator exists
    assert 'id="thinking"' in html_content, \
        "Thinking indicator element should exist"
    
    # Check that it's inside the input-area div by finding content between
    # the opening and closing of input-area (accounting for nested divs)
    input_area_start = html_content.find('<div class="input-area">')
    # Find the specific closing tag for input-area (not the nested one)
    # The input-area div contains input-controls div, so we need to find the second closing div
    input_area_end = html_content.find('</div>', input_area_start + len('<div class="input-area">'))
    # Now find the next closing div after that
    input_area_end = html_content.find('</div>', input_area_end + 1)
    
    input_area_html = html_content[input_area_start:input_area_end + 6]
    
    assert 'id="thinking"' in input_area_html, \
        "Thinking indicator should be inside the input-area div"
    
    # Check that it has the thinking-indicator class
    assert '<div class="thinking-indicator" id="thinking">' in html_content, \
        "Thinking indicator should have thinking-indicator class"
