"""Anthropic Messages API wrapper with usage tracking and compatible-profile support."""

from __future__ import annotations

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
        inp = getattr(usage, "input_tokens", 0) or 0
        out = getattr(usage, "output_tokens", 0) or 0
        self.input_tokens += inp
        self.output_tokens += out
        self.cache_read += getattr(usage, "cache_read_input_tokens", 0) or 0
        self.cache_write += getattr(usage, "cache_creation_input_tokens", 0) or 0
        self.calls += 1
        prev = self.by_model.get(model, (0, 0))
        self.by_model[model] = (prev[0] + inp, prev[1] + out)

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


def _resolve_api_key() -> str | None:
    """Prefer ANTHROPIC_API_KEY; fall back to ANTHROPIC_AUTH_TOKEN (compatible gateways)."""
    return os.getenv("ANTHROPIC_API_KEY") or os.getenv("ANTHROPIC_AUTH_TOKEN") or None


class LLM:
    def __init__(self, cfg: ModelConfig, usage: Usage | None = None, stream: bool = True) -> None:
        self.cfg = cfg
        self.usage = usage or Usage()
        self.stream = stream
        kwargs: dict[str, Any] = {
            "max_retries": 3,
            "timeout": anthropic.Timeout(600.0, connect=15.0, read=60.0, write=30.0),
        }
        if cfg.base_url:
            kwargs["base_url"] = cfg.base_url
        api_key = _resolve_api_key()
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)

    def _extras(self, model: str, effort: str | None) -> dict[str, Any]:
        """First-party-only fields; dropped for third-party compatible profiles."""
        if self.cfg.profile != "anthropic":
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
    ) -> anthropic.types.Message:
        model = model or self.cfg.main
        payload: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens or self.cfg.max_tokens,
            "system": system,
            "messages": messages,
            **self._extras(model, effort),
        }
        if tools:
            payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice

        if self.stream:
            with self.client.messages.stream(**payload) as stream:
                response = stream.get_final_message()
        else:
            response = self.client.messages.create(**payload)
        self.usage.add(model, response.usage)
        return response

    def count_tokens(
        self,
        *,
        system: list[dict[str, Any]] | str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
    ) -> int | None:
        try:
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
