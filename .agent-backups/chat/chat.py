#!/usr/bin/env python3
"""Interactive chat agent with web search, on a local OpenAI-compatible server.

A single researcher agent in a REPL: multi-turn conversation, two tools
(web_search via DuckDuckGo — no API key — and fetch_page for reading a URL),
with the same robustness tricks as the coding team: text tool-call fallback,
context trimming, bounded tool rounds.

Usage:
    pip install -r chat/requirements.txt
    python chat/chat.py                     # uses chat/chat.json
    python chat/chat.py --model qwen3:4b    # override the model
"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from html.parser import HTMLParser
from pathlib import Path

from openai import OpenAI

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "orchestrator"))
from run_team import parse_text_tool_calls, summarize_args  # noqa: E402

CHAT_TOOLS = {"web_search", "fetch_page"}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (DuckDuckGo). Returns titles, URLs and snippets.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Short keyword query, 2-6 terms."},
                    "max_results": {"type": "integer", "description": "How many results (default 5)."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "Fetch a URL and return its readable text content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "The http(s) URL to read."}
                },
                "required": ["url"],
            },
        },
    },
]


def web_search(query: str, max_results: int = 5) -> str:
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS  # older package name
    except ImportError:
        return ("ERROR: the search package is not installed. "
                "Tell the user to run: pip install -r chat/requirements.txt")
    try:
        results = list(DDGS().text(query, max_results=max_results))
    except Exception as exc:
        return f"ERROR: search failed: {exc}"
    if not results:
        return f"no results for query: {query!r}"
    return "\n\n".join(
        f"{r.get('title', '?')}\n{r.get('href', '?')}\n{r.get('body', '')}"
        for r in results
    )


class TextExtractor(HTMLParser):
    """Crude but dependency-free HTML -> readable text."""
    SKIP = {"script", "style", "noscript", "svg", "header", "footer", "nav", "form"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip_depth += 1

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip_depth:
            self._skip_depth -= 1

    def handle_data(self, data):
        if not self._skip_depth and data.strip():
            self.parts.append(" ".join(data.split()))


def fetch_page(url: str, max_chars: int = 8000) -> str:
    if not url.startswith(("http://", "https://")):
        return f"ERROR: not an http(s) URL: {url}"
    request = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; local-chat-agent)"}
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(600_000)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        return f"ERROR: could not fetch {url}: {exc}"
    parser = TextExtractor()
    try:
        parser.feed(raw.decode("utf-8", errors="replace"))
    except Exception as exc:
        return f"ERROR: could not parse {url}: {exc}"
    text = "\n".join(parser.parts)
    if len(text) > max_chars:
        text = text[:max_chars] + f"\n... [truncated, page has {len(text)} chars of text]"
    return text or f"(no readable text found at {url})"


def dispatch(name: str, args: dict) -> str:
    try:
        if name == "web_search":
            return web_search(args["query"], int(args.get("max_results", 5)))
        if name == "fetch_page":
            return fetch_page(args["url"])
        return f"ERROR: unknown tool {name}"
    except Exception as exc:
        return f"ERROR: {exc}"


def shrink(messages: list, budget: int) -> None:
    """Trim old tool output so long conversations stay fast on a local model."""
    def total() -> int:
        return sum(len(str(m.get("content") or "")) for m in messages if isinstance(m, dict))
    if total() <= budget:
        return
    for m in messages[1:-4]:
        if not isinstance(m, dict):
            continue
        content = str(m.get("content") or "")
        is_tool_output = m.get("role") == "tool" or (
            m.get("role") == "user" and content.startswith("Result of ")
        )
        if is_tool_output and len(content) > 400:
            m["content"] = content[:300] + "\n[... older tool output trimmed ...]"
            if total() <= budget:
                break


def answer_turn(client: OpenAI, config: dict, messages: list) -> str:
    """One user turn: tool loop until the model produces a normal reply."""
    seen: dict = {}
    for _ in range(config.get("max_tool_rounds", 8)):
        shrink(messages, config.get("max_context_chars", 60_000))
        response = client.chat.completions.create(
            model=config["model"],
            messages=messages,
            temperature=config.get("temperature", 0.6),
            tools=TOOL_SCHEMAS,
        )
        msg = response.choices[0].message

        calls = []
        if msg.tool_calls:
            messages.append(msg)
            for call in msg.tool_calls:
                try:
                    args = json.loads(call.function.arguments or "{}")
                except json.JSONDecodeError:
                    args = {}
                calls.append(("native", call.id, call.function.name, args))
        else:
            text_calls = parse_text_tool_calls(msg.content or "", CHAT_TOOLS)
            if not text_calls:
                return msg.content or ""
            messages.append({"role": "assistant", "content": msg.content})
            calls = [("text", None, name, args) for name, args in text_calls]

        text_results = []
        for kind, call_id, name, args in calls:
            print(f"  [{name}] {summarize_args(args)}")
            signature = name + json.dumps(args, sort_keys=True)
            seen[signature] = seen.get(signature, 0) + 1
            if seen[signature] > 2:
                result = ("REPEATED CALL: you already made this exact call. "
                          "Refine the query or answer with what you have.")
            else:
                result = dispatch(name, args)
            if kind == "native":
                messages.append({"role": "tool", "tool_call_id": call_id, "content": result})
            else:
                text_results.append(f"Result of {name}:\n{result}")
        if text_results:
            messages.append({
                "role": "user",
                "content": "\n\n".join(text_results)
                + "\n\nContinue. When you have enough, answer the user in plain text (no JSON).",
            })

    messages.append({
        "role": "user",
        "content": "Tool budget for this turn is used up. Answer now with what you have, "
        "and be explicit about anything you could not verify.",
    })
    response = client.chat.completions.create(
        model=config["model"], messages=messages,
        temperature=config.get("temperature", 0.6),
    )
    return response.choices[0].message.content or ""


def main():
    parser = argparse.ArgumentParser(description="Local chat agent with web search.")
    parser.add_argument("--config", default=str(REPO_ROOT / "chat" / "chat.json"))
    parser.add_argument("--model", default=None, help="Override the model from the config.")
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text())
    if args.model:
        config["model"] = args.model
    system_prompt = (REPO_ROOT / config["system_prompt"]).read_text()

    client = OpenAI(
        base_url=config["server"]["base_url"],
        api_key=config["server"].get("api_key", "local"),
        timeout=config["server"].get("request_timeout_seconds", 300),
        max_retries=1,
    )
    messages: list = [{"role": "system", "content": system_prompt}]

    print(f"chat agent ready (model: {config['model']}). "
          "/clear resets the conversation, /exit quits.")
    while True:
        try:
            user_input = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not user_input:
            continue
        if user_input in ("/exit", "/quit"):
            break
        if user_input == "/clear":
            messages = [{"role": "system", "content": system_prompt}]
            print("(conversation cleared)")
            continue

        messages.append({"role": "user", "content": user_input})
        try:
            reply = answer_turn(client, config, messages)
        except KeyboardInterrupt:
            print("\n(interrupted — that turn was abandoned)")
            messages.pop()
            continue
        messages.append({"role": "assistant", "content": reply})
        print(f"\n{reply}")


if __name__ == "__main__":
    main()
