"""parse_text_tool_calls: recovering tool calls a model printed as text."""

from run_team import parse_text_tool_calls, KNOWN_TOOLS
from chat.chat import CHAT_TOOLS


def test_fenced_json_call():
    text = 'Sure.\n```json\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x"}}\n```'
    assert parse_text_tool_calls(text) == [("write_file", {"path": "a.py", "content": "x"})]


def test_unfenced_call():
    text = 'I will run it {"name": "run_command", "arguments": {"command": "ls"}} now'
    assert parse_text_tool_calls(text) == [("run_command", {"command": "ls"})]


def test_multiple_calls_in_one_reply():
    text = (
        '```json\n{"name": "write_file", "arguments": {"path": "a", "content": "1"}}\n```\n'
        '```json\n{"name": "run_command", "arguments": {"command": "cat a"}}\n```'
    )
    calls = parse_text_tool_calls(text)
    assert [c[0] for c in calls] == ["write_file", "run_command"]


def test_nested_function_wrapper():
    text = '{"function": {"name": "list_files", "arguments": {}}}'
    assert parse_text_tool_calls(text) == [("list_files", {})]


def test_parameters_alias_and_string_args():
    text = '{"name": "read_file", "parameters": "{\\"path\\": \\"b.py\\"}"}'
    assert parse_text_tool_calls(text) == [("read_file", {"path": "b.py"})]


def test_unknown_tool_is_ignored():
    text = '{"name": "rm_rf_everything", "arguments": {}}'
    assert parse_text_tool_calls(text) == []


def test_known_tools_scoping():
    # A chat reply must not surface coding tools, and vice versa.
    coding = '{"name": "run_command", "arguments": {"command": "ls"}}'
    assert parse_text_tool_calls(coding, CHAT_TOOLS) == []
    search = '{"name": "web_search", "arguments": {"query": "x"}}'
    assert parse_text_tool_calls(search, CHAT_TOOLS) == [("web_search", {"query": "x"})]
    assert parse_text_tool_calls(search, KNOWN_TOOLS) == []


def test_plain_prose_yields_nothing():
    assert parse_text_tool_calls("Here is your answer, no tools needed.") == []
