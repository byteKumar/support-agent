"""LLM client interface.

AnthropicLLM is the only class that talks to the paid API, and it is only built
when LLM_PROVIDER=anthropic. Tests, dev runs and CI use FakeLLM, which plays back
pre-scripted assistant turns (tool_use or text) and costs nothing.
"""

from __future__ import annotations

import copy
import itertools
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

from support_agent.config import Settings


@dataclass
class Usage:
    input_tokens: int
    output_tokens: int


@dataclass
class LLMResponse:
    content: list[dict]  # content blocks as plain JSON dicts, safe to persist and resend
    stop_reason: str
    usage: Usage
    model: str


class LLMClient(Protocol):
    model: str

    def create(
        self, *, system: str, tools: list[dict], messages: list[dict], max_tokens: int
    ) -> LLMResponse: ...


class AnthropicLLM:
    """Real Claude client. One request per agent step; the agent loop owns retries."""

    def __init__(self, model: str, client: Any = None, effort: str = "low"):
        if client is None:
            import anthropic

            # max_retries=0: the agent loop retries itself so every attempt is traced.
            client = anthropic.Anthropic(max_retries=0)
        self._client = client
        self.model = model
        self.effort = effort

    def create(self, *, system, tools, messages, max_tokens):
        resp = self._client.messages.create(
            model=self.model,
            max_tokens=max_tokens,
            system=system,
            tools=tools,
            messages=messages,
            output_config={"effort": self.effort},
        )
        content = [block.model_dump(mode="json", exclude_none=True) for block in resp.content]
        return LLMResponse(
            content=content,
            stop_reason=resp.stop_reason,
            usage=Usage(resp.usage.input_tokens, resp.usage.output_tokens),
            model=getattr(resp, "model", self.model),
        )


# ---------------------------------------------------------------- fake


@dataclass
class ScriptedTurn:
    content: list[dict]
    stop_reason: str
    input_tokens: int = 1200
    output_tokens: int = 150


def text_turn(text: str, **kw) -> ScriptedTurn:
    return ScriptedTurn([{"type": "text", "text": text}], "end_turn", **kw)


def tool_turn(*calls: tuple[str, dict], text: str | None = None, **kw) -> ScriptedTurn:
    blocks: list[dict] = [{"type": "text", "text": text}] if text else []
    blocks += [{"type": "tool_use", "id": None, "name": n, "input": i} for n, i in calls]
    return ScriptedTurn(blocks, "tool_use", **kw)


ScriptItem = ScriptedTurn | Exception | Callable[[list[dict]], ScriptedTurn]


@dataclass
class FakeLLM:
    """Plays back a script. Items can be turns, exceptions to raise, or callables
    that look at the conversation and return a turn."""

    script: list[ScriptItem]
    model: str = "fake-scripted"
    calls: list[dict] = field(default_factory=list)
    _ids: Any = field(default_factory=lambda: itertools.count(1))

    def create(self, *, system, tools, messages, max_tokens):
        self.calls.append({"system": system, "tools": tools, "messages": copy.deepcopy(messages)})
        if not self.script:
            raise RuntimeError("FakeLLM script exhausted")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        turn = item(messages) if callable(item) else item
        content = copy.deepcopy(turn.content)
        for block in content:
            if block["type"] == "tool_use" and not block.get("id"):
                block["id"] = f"toolu_fake_{next(self._ids):04d}"
        return LLMResponse(
            content=content,
            stop_reason=turn.stop_reason,
            usage=Usage(turn.input_tokens, turn.output_tokens),
            model=self.model,
        )


class EscalateEverythingLLM:
    """Fake 'model' that escalates every ticket. Used as the dev-server default and as a
    floor baseline when smoke-testing the eval harness without an API key."""

    model = "fake-escalate-everything"

    def __init__(self):
        self._ids = itertools.count(1)

    def create(self, *, system, tools, messages, max_tokens):
        last = messages[-1]
        already = isinstance(last["content"], list) and any(
            b.get("type") == "tool_result" for b in last["content"]
        )
        if already:
            content = [{"type": "text", "text": "Escalated to a human agent."}]
            return LLMResponse(content, "end_turn", Usage(0, 0), self.model)
        block = {
            "type": "tool_use",
            "id": f"toolu_esc_{next(self._ids):04d}",
            "name": "escalate_to_human",
            "input": {"reason": "fake provider: no model configured", "category": "other"},
        }
        return LLMResponse([block], "tool_use", Usage(0, 0), self.model)


def make_llm(settings: Settings) -> LLMClient:
    if settings.llm_provider == "anthropic":
        return AnthropicLLM(settings.llm_model)
    return EscalateEverythingLLM()
