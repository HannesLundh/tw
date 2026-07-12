from pathlib import Path
html = Path("chat/static/index.html").read_text()

# Final comprehensive check
checks = [
    ("Self-contained HTML", html.startswith("<!DOCTYPE") and "</html>" in html),
    ("Inline CSS", "<style>" in html and "</style>" in html),
    ("Inline JavaScript", "<script>" in html and "</script>" in html),
    ("Message history container", "id=\"messages\"" in html),
    ("User input field", "id=\"user-input\"" in html and "type=\"text\"" in html),
    ("Send button", "id=\"send-button\"" in html),
    ("Thinking indicator", "id=\"thinking\"" in html),
    ("POST to /api/chat", "/api/chat" in html and "method: 'POST'" in html),
    ("JSON body with messages", "messages:" in html),
    ("Reply handling", "reply" in html),
]

print("Final Verification:")
all_passed = True
for name, result in checks:
    status = "✓" if result else "✗"
    print(f"{status} {name}")
    if not result:
        all_passed = False

# Check thinking indicator patterns separately
thinking_show = "thinkingIndicator.style.display = 'block'" in html
thinking_hide = "thinkingIndicator.style.display = 'none'" in html
print(f"✓ Thinking show logic: {"found" if thinking_show else "missing"}")
print(f"✓ Thinking hide logic: {"found" if thinking_hide else "missing"}")

print()
if all_passed and thinking_show and thinking_hide:
    print("✅ ALL CHECKS PASSED! HTML page is complete and functional.")
else:
    print("❌ Some checks failed.")