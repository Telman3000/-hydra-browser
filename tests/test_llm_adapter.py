"""Anthropic-shaped transcript -> OpenAI chat messages."""

import json

from hydra.llm import ContentBlock, _anthropic_messages_to_openai


def test_tool_call_without_arguments_converts():
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "go back"}]},
        {
            "role": "assistant",
            "content": [
                ContentBlock(type="text", text=""),
                ContentBlock(type="tool_use", id="call_1", name="browser_back", input={}),
            ],
        },
        {
            "role": "user",
            "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "ok"}],
        },
    ]
    out = _anthropic_messages_to_openai(messages)
    assistant = next(m for m in out if m["role"] == "assistant")
    call = assistant["tool_calls"][0]
    assert call["function"]["name"] == "browser_back"
    assert json.loads(call["function"]["arguments"]) == {}
    assert any(m["role"] == "tool" and m["tool_call_id"] == "call_1" for m in out)


def test_worker_out_of_steps_still_reports():
    from hydra.agent.workers import _salvage_report
    from hydra.llm import SimpleMessage

    seen = {}

    class StubLLM:
        def create(self, *, system, messages, tools, max_tokens):
            seen["last_roles"] = [m["role"] for m in messages[-2:]]
            return SimpleMessage(
                content=[ContentBlock(type="tool_use", id="f", name="finish", input={"report": "found X"})]
            )

    history = [
        {"role": "user", "content": [{"type": "text", "text": "goal"}]},
        {"role": "assistant", "content": [ContentBlock(type="tool_use", id="a", name="note", input={})]},
    ]
    assert _salvage_report(StubLLM(), history, []) == "found X"
    assert seen["last_roles"] == ["user", "user"]


def test_dict_blocks_convert_too():
    messages = [
        {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "plan"},
                {"type": "tool_use", "id": "c2", "name": "note", "input": {"key": "a"}},
            ],
        }
    ]
    out = _anthropic_messages_to_openai(messages)
    assert out[0]["content"] == "plan"
    assert json.loads(out[0]["tool_calls"][0]["function"]["arguments"]) == {"key": "a"}
