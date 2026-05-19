"""Unit tests for Codex-Ollama Protocol Bridge — pure functions only."""

import pytest
import sys
sys.path.insert(0, ".")

from proxy import normalize_usage, simplify_tools, responses_to_chat


# ── normalize_usage ──────────────────────────────────────────────────────────

class TestNormalizeUsage:

    def test_ollama_format(self):
        """Ollama keys (prompt_tokens, completion_tokens) → OpenAI keys."""
        assert normalize_usage({
            "prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150
        }) == {"input_tokens": 100, "output_tokens": 50, "total_tokens": 150}

    def test_openai_format_passthrough(self):
        """Already OpenAI-format usage passes through unchanged."""
        assert normalize_usage({
            "input_tokens": 200, "output_tokens": 80, "total_tokens": 280
        }) == {"input_tokens": 200, "output_tokens": 80, "total_tokens": 280}

    def test_empty_dict(self):
        assert normalize_usage({}) == {}

    def test_none(self):
        assert normalize_usage(None) == {}

    def test_missing_total(self):
        """Missing total_tokens defaults to 0."""
        assert normalize_usage({"prompt_tokens": 10}) == {
            "input_tokens": 10, "total_tokens": 0
        }


# ── simplify_tools ───────────────────────────────────────────────────────────

SAMPLE_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "exec_command",
            "description": "Run a shell command",
            "parameters": {
                "type": "object",
                "properties": {
                    "cmd": {"type": "string", "description": "The command"},
                    "workdir": {"type": "string", "description": "Working dir"},
                    "timeout": {"type": "integer", "description": "Timeout ms"},
                    "env": {"type": "object", "description": "Env vars"},
                },
                "required": ["cmd", "timeout", "env"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "view_image",
            "description": "View an image",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Image path"},
                    "detail": {"type": "string", "description": "Detail level"},
                },
                "required": ["path", "detail"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unknown_tool",
            "description": "A tool not in the essential list",
            "parameters": {
                "type": "object",
                "properties": {
                    "foo": {"type": "string", "description": "Some param"},
                    "bar": {"type": "integer", "description": "Another param"},
                },
                "required": ["foo", "bar"],
            },
        },
    },
]


class TestSimplifyTools:

    def test_known_tool_stripped_to_essential(self):
        result = simplify_tools(SAMPLE_TOOLS)
        exec_cmd = [t for t in result
                    if t["function"]["name"] == "exec_command"][0]
        props = exec_cmd["function"]["parameters"]["properties"]
        required = exec_cmd["function"]["parameters"]["required"]

        assert set(props.keys()) == {"cmd", "workdir"}
        assert required == ["cmd"]  # workdir not required
        # Each property keeps type + description
        assert props["cmd"]["type"] == "string"

    def test_known_tool_single_param(self):
        result = simplify_tools(SAMPLE_TOOLS)
        vi = [t for t in result if t["function"]["name"] == "view_image"][0]
        props = vi["function"]["parameters"]["properties"]

        assert set(props.keys()) == {"path"}
        # detail was required by sample but not essential → stripped
        assert vi["function"]["parameters"]["required"] == ["path"]

    def test_unknown_tool_untouched(self):
        result = simplify_tools(SAMPLE_TOOLS)
        unk = [t for t in result if t["function"]["name"] == "unknown_tool"][0]
        props = unk["function"]["parameters"]["properties"]

        assert set(props.keys()) == {"foo", "bar"}
        assert unk["function"]["parameters"]["required"] == ["foo", "bar"]

    def test_empty_list(self):
        assert simplify_tools([]) == []

    def test_tool_without_function_name(self):
        """Tools without a function name are skipped."""
        tools = [{"type": "function", "function": {"parameters": {}}}]
        assert simplify_tools(tools) == []


# ── responses_to_chat ────────────────────────────────────────────────────────

RESPONSES_BODY_SIMPLE = {
    "model": "qwen3",
    "input": "List files in current directory",
    "instructions": "You are a helpful coding assistant.",
    "tools": [],
}


class TestResponsesToChat:

    def test_basic_conversion(self):
        chat = responses_to_chat(RESPONSES_BODY_SIMPLE)
        assert chat["model"] == "qwen3"
        assert chat["stream"] is False
        assert len(chat["messages"]) == 2  # system + user
        assert chat["messages"][0]["role"] == "system"
        assert "You are a helpful coding assistant." in chat["messages"][0]["content"]
        assert chat["messages"][1]["role"] == "user"
        assert chat["messages"][1]["content"] == "List files in current directory"

    def test_instructions_in_system_prompt(self):
        chat = responses_to_chat(RESPONSES_BODY_SIMPLE)
        system = chat["messages"][0]["content"]
        assert "You are a helpful coding assistant." in system

    def test_tool_system_prompt_appended(self):
        chat = responses_to_chat(RESPONSES_BODY_SIMPLE)
        system = chat["messages"][0]["content"]
        assert "CRITICAL" in system
        assert "MUST call one of the provided tools" in system

    def test_no_instructions_default_system_prompt(self):
        body = {"model": "qwen3", "input": "hi", "tools": []}
        chat = responses_to_chat(body)
        assert "coding agent" in chat["messages"][0]["content"]

    def test_string_input_becomes_user_message(self):
        chat = responses_to_chat({"model": "m", "input": "hello"})
        assert chat["messages"][-1] == {"role": "user", "content": "hello"}

    def test_input_list_with_messages(self):
        body = {
            "model": "m",
            "input": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "input_text", "text": "OK doing it."}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Now run tests."}],
                },
            ],
        }
        chat = responses_to_chat(body)
        msgs = chat["messages"]
        assert msgs[0]["role"] == "system"
        assert msgs[1] == {"role": "assistant", "content": "OK doing it."}
        assert msgs[2] == {"role": "user", "content": "Now run tests."}

    def test_input_list_plain_strings(self):
        body = {"model": "m", "input": ["do this", "then that"]}
        chat = responses_to_chat(body)
        user_msgs = [m for m in chat["messages"] if m["role"] == "user"]
        assert len(user_msgs) == 2
        assert user_msgs[0]["content"] == "do this"
        assert user_msgs[1]["content"] == "then that"

    def test_tool_choice_set_when_tools_present(self):
        body = {
            "model": "m",
            "input": "run command",
            "tools": [{"name": "exec_command", "description": "Run shell"}],
        }
        chat = responses_to_chat(body)
        assert "tools" in chat
        assert chat["tool_choice"] == "auto"

    def test_no_tool_choice_when_no_tools(self):
        chat = responses_to_chat(RESPONSES_BODY_SIMPLE)
        assert "tools" not in chat
        assert "tool_choice" not in chat

    def test_tool_without_function_key_normalized(self):
        """Tool with 'name' at top level gets wrapped in 'function' key."""
        body = {
            "model": "m",
            "input": "x",
            "tools": [{
                "name": "view_image",
                "description": "View image",
                "parameters": {"type": "object", "properties": {}, "required": []},
            }],
        }
        chat = responses_to_chat(body)
        tool = chat["tools"][0]
        assert tool["type"] == "function"
        assert tool["function"]["name"] == "view_image"
