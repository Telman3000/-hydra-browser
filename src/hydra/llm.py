"""LLM client: Anthropic Messages API or OpenAI Chat Completions (tool calling).

The agent loop always sees Anthropic-shaped content blocks (tool_use / text), so
OpenAI responses are adapted to that surface.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Iterable

import anthropic

from .config import ModelConfig

log = logging.getLogger(__name__)

PRICES: dict[str, tuple[float, float]] = {
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-sonnet-5": (2.0, 10.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.5, 10.0),
    "gpt-4.1-mini": (0.40, 1.60),
    "gpt-4.1": (2.0, 8.0),
}


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read: int = 0
    cache_write: int = 0
    calls: int = 0
    by_model: dict[str, tuple[int, int]] = field(default_factory=dict)

    def add(self, model: str, usage: Any) -> None:
        inp = (
            getattr(usage, "input_tokens", None)
            or getattr(usage, "prompt_tokens", None)
            or 0
        )
        out = (
            getattr(usage, "output_tokens", None)
            or getattr(usage, "completion_tokens", None)
            or 0
        )
        self.input_tokens += int(inp)
        self.output_tokens += int(out)
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.calls += 1
        prev = self.by_model.get(model, (0, 0))
        self.by_model[model] = (prev[0] + int(inp), prev[1] + int(out))

    @property
    def priced(self) -> bool:
        return any(m in PRICES for m in self.by_model)

    @property
    def cost_usd(self) -> float:
        total = 0.0
        for model, (inp, out) in self.by_model.items():
            pin, pout = PRICES.get(model, (0.0, 0.0))
            total += inp / 1e6 * pin + out / 1e6 * pout
        return total


@dataclass
class ContentBlock:
    type: str
    text: str = ""
    id: str = ""
    name: str = ""
    input: dict[str, Any] = field(default_factory=dict)
    thinking: str = ""


@dataclass
class SimpleUsage:
    input_tokens: int = 0
    output_tokens: int = 0


@dataclass
class SimpleMessage:
    content: list[ContentBlock]
    stop_reason: str = "tool_use"
    usage: SimpleUsage = field(default_factory=SimpleUsage)


def _resolve_anthropic_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or None


def _system_text(system: list[dict[str, Any]] | str) -> str:
    if isinstance(system, str):
        return system
    parts = []
    for block in system:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _anthropic_tools_to_openai(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not tools:
        return []
    out = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t.get("input_schema")
                    or {"type": "object", "properties": {}},
                },
            }
        )
    return out


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    # Should not appear as assistant content; handled separately.
                    body = block.get("content", "")
                    parts.append(body if isinstance(body, str) else json.dumps(body))
                elif block.get("type") == "image":
                    parts.append("[image]")
            else:
                t = getattr(block, "type", None)
                if t == "text":
                    parts.append(getattr(block, "text", ""))
        return "\n".join(p for p in parts if p)
    return str(content)


def field_of(block: Any, key: str, default: Any = None) -> Any:
    """Read a content-block field whether the block is a dict or an SDK object."""
    if isinstance(block, dict):
        return block.get(key, default)
    return getattr(block, key, default)


def _anthropic_messages_to_openai(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Map Anthropic transcript (tool_use / tool_result) → OpenAI chat messages."""
    out: list[dict[str, Any]] = []
    for message in messages:
        role = message["role"]
        content = message.get("content")

        if role == "assistant":
            text_parts: list[str] = []
            tool_calls = []
            blocks = content if isinstance(content, list) else []
            for block in blocks:
                btype = field_of(block, "type")
                if btype == "text":
                    text_parts.append(field_of(block, "text") or "")
                elif btype == "tool_use":
                    name = field_of(block, "name")
                    bid = field_of(block, "id")
                    args = field_of(block, "input") or {}
                    tool_calls.append(
                        {
                            "id": bid,
                            "type": "function",
                            "function": {
                                "name": name,
                                "arguments": json.dumps(args, ensure_ascii=False),
                            },
                        }
                    )
            msg: dict[str, Any] = {"role": "assistant", "content": "\n".join(text_parts) or None}
            if tool_calls:
                msg["tool_calls"] = tool_calls
            out.append(msg)
            continue

        # user
        if isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        if not isinstance(content, list):
            out.append({"role": "user", "content": str(content)})
            continue

        tool_results = [
            b for b in content if isinstance(b, dict) and b.get("type") == "tool_result"
        ]
        texts = [b for b in content if isinstance(b, dict) and b.get("type") == "text"]
        other = [
            b
            for b in content
            if not (isinstance(b, dict) and b.get("type") in ("tool_result", "text"))
        ]

        if tool_results and not texts and not other:
            for tr in tool_results:
                body = tr.get("content", "")
                if isinstance(body, list):
                    body = _content_to_text(body)
                elif not isinstance(body, str):
                    body = json.dumps(body, ensure_ascii=False)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": tr.get("tool_use_id", ""),
                        "content": body or "",
                    }
                )
            continue

        # Mixed user turn: text first, then tool results as separate tool messages.
        if texts or other:
            out.append({"role": "user", "content": _content_to_text(content)})
        for tr in tool_results:
            body = tr.get("content", "")
            if isinstance(body, list):
                body = _content_to_text(body)
            elif not isinstance(body, str):
                body = json.dumps(body, ensure_ascii=False)
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": tr.get("tool_use_id", ""),
                    "content": body or "",
                }
            )
    return out


class LLM:
    def __init__(self, cfg: ModelConfig, usage: Usage | None = None, stream: bool = True) -> None:
        self.cfg = cfg
        self.usage = usage or Usage()
        self.stream = stream
        self.provider = (cfg.provider or "anthropic").lower()
        self._openai = None
        self.client = None

        if self.provider == "openai":
            from openai import OpenAI

            # Parallel workers share one per-minute token quota; the SDK honours the
            # provider's retry-after hints, so a few extra retries ride out bursts.
            kwargs: dict[str, Any] = {"max_retries": 6}
            if cfg.base_url and "z.ai" not in (cfg.base_url or ""):
                # Only use base_url for OpenAI if it's not the leftover z.ai anthropic URL.
                if "openai" in cfg.base_url or "azure" in cfg.base_url:
                    kwargs["base_url"] = cfg.base_url
            key = os.getenv("OPENAI_API_KEY")
            if key:
                kwargs["api_key"] = key
            self._openai = OpenAI(**kwargs)
        else:
            kwargs = {
                "max_retries": 3,
                "timeout": anthropic.Timeout(600.0, connect=15.0, read=60.0, write=30.0),
            }
            if cfg.base_url:
                kwargs["base_url"] = cfg.base_url
            api_key = _resolve_anthropic_key()
            if api_key:
                kwargs["api_key"] = api_key
            self.client = anthropic.Anthropic(**kwargs)

    def _extras(self, model: str, effort: str | None) -> dict[str, Any]:
        if self.cfg.profile != "anthropic" or self.provider != "anthropic":
            return {}
        extras: dict[str, Any] = {}
        if model.startswith(
            ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable", "claude-sonnet-4")
        ):
            extras["thinking"] = {"type": "adaptive"}
            extras["output_config"] = {"effort": effort or self.cfg.effort}
        return extras

    def create(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        effort: str | None = None,
        tool_choice: dict[str, Any] | None = None,
    ) -> Any:
        if self.provider == "openai":
            return self._create_openai(
                system=system,
                messages=messages,
                tools=tools,
                model=model,
                max_tokens=max_tokens,
            )
        return self._create_anthropic(
            system=system,
            messages=messages,
            tools=tools,
            model=model,
            max_tokens=max_tokens,
            effort=effort,
            tool_choice=tool_choice,
        )

    def _create_anthropic(self, **kwargs: Any) -> Any:
        model = kwargs.get("model") or self.cfg.main
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": kwargs.get("max_tokens") or self.cfg.max_tokens,
            "system": kwargs["system"],
            "messages": kwargs["messages"],
            **self._extras(model, kwargs.get("effort")),
        }
        if kwargs.get("tools"):
            payload["tools"] = kwargs["tools"]
        if kwargs.get("tool_choice"):
            payload["tool_choice"] = kwargs["tool_choice"]

        assert self.client is not None
        if self.stream:
            with self.client.messages.stream(**payload) as stream:
                response = stream.get_final_message()
        else:
            response = self.client.messages.create(**payload)
        self.usage.add(model, response.usage)
        return response

    def _create_openai(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        model: str | None,
        max_tokens: int | None,
    ) -> SimpleMessage:
        assert self._openai is not None
        model = model or self.cfg.main
        oai_messages = [{"role": "system", "content": _system_text(system)}]
        oai_messages.extend(_anthropic_messages_to_openai(messages))
        payload: dict[str, Any] = {
            "model": model,
            "messages": oai_messages,
            "max_completion_tokens": max_tokens or self.cfg.max_tokens,
        }
        oai_tools = _anthropic_tools_to_openai(tools)
        if oai_tools:
            payload["tools"] = oai_tools
            payload["tool_choice"] = "auto"
            # A batch of clicks planned against one snapshot goes stale after the
            # first one changes the page; one call per turn keeps refs valid.
            payload["parallel_tool_calls"] = False

        response = self._openai.chat.completions.create(**payload)
        choice = response.choices[0].message
        blocks: list[ContentBlock] = []
        if choice.content:
            blocks.append(ContentBlock(type="text", text=choice.content))
        for call in choice.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"raw": call.function.arguments}
            blocks.append(
                ContentBlock(
                    type="tool_use",
                    id=call.id,
                    name=call.function.name,
                    input=args if isinstance(args, dict) else {"value": args},
                )
            )
        usage = SimpleUsage(
            input_tokens=getattr(response.usage, "prompt_tokens", 0) or 0,
            output_tokens=getattr(response.usage, "completion_tokens", 0) or 0,
        )
        self.usage.add(model, usage)
        stop = "tool_use" if any(b.type == "tool_use" for b in blocks) else "end_turn"
        return SimpleMessage(content=blocks, stop_reason=stop, usage=usage)

    def count_tokens(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> int | None:
        if self.provider == "openai":
            return None
        try:
            assert self.client is not None
            payload: dict[str, Any] = {
                "model": model or self.cfg.main,
                "system": system,
                "messages": messages,
            }
            if tools:
                payload["tools"] = tools
            return self.client.messages.count_tokens(**payload).input_tokens
        except Exception as exc:  # noqa: BLE001
            log.debug("count_tokens unavailable: %s", exc)
            return None

    def text(
        self,
        *,
        system: str,
        prompt: str,
        model: str | None = None,
        max_tokens: int = 1_500,
        effort: str | None = "low",
    ) -> str:
        response = self.create(
            system=system,
            messages=[{"role": "user", "content": prompt}],
            model=model or self.cfg.small,
            max_tokens=max_tokens,
            effort=effort,
        )
        return text_of(response.content)


def text_of(blocks: Iterable[Any]) -> str:
    return "\n".join(b.text for b in blocks if getattr(b, "type", None) == "text").strip()
