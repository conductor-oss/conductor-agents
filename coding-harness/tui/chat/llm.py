"""The conductor agent: an AsyncAnthropic streaming tool-use loop.

Drives one user turn to completion — streams assistant text, runs any tools the model
calls (via the supplied `run_tool`), feeds results back, and loops until the model stops.
Mutates `messages` in place (Anthropic block format) so the caller can persist/resume it.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from .tools import TOOLS

# rough $/Mtok for a cost estimate (input, output); default = sonnet-ish
_RATES = {
    "opus": (15.0, 75.0),
    "sonnet": (3.0, 15.0),
    "haiku": (0.8, 4.0),
}
MAX_TOKENS = 4096


def _rate(model: str) -> tuple[float, float]:
    m = (model or "").lower()
    for key, r in _RATES.items():
        if key in m:
            return r
    return _RATES["sonnet"]


def _assistant_blocks(final) -> list[dict]:
    """Serialize an Anthropic response's content into plain dicts (re-sendable + storable)."""
    blocks: list[dict] = []
    for b in final.content:
        if b.type == "text":
            blocks.append({"type": "text", "text": b.text})
        elif b.type == "tool_use":
            blocks.append({"type": "tool_use", "id": b.id, "name": b.name, "input": b.input})
    return blocks


class ChatEngine:
    def __init__(self, client, model: str, system: str):
        self._client = client
        self.model = model
        self._system = system

    async def _stream_once(self, messages, on_text):
        """One model call: stream text, return the final message. Retries transient errors."""
        import anthropic
        last = None
        for attempt in range(4):
            try:
                async with self._client.messages.stream(
                    model=self.model, max_tokens=MAX_TOKENS, system=self._system,
                    tools=TOOLS, messages=messages,
                ) as stream:
                    async for text in stream.text_stream:
                        on_text(text)
                    return await stream.get_final_message()
            except (anthropic.APIConnectionError, anthropic.APITimeoutError,
                    anthropic.InternalServerError, anthropic.RateLimitError) as e:
                last = e
                await asyncio.sleep(1.5 * (attempt + 1))
        raise last  # exhausted

    async def run(
        self,
        messages: list,
        *,
        run_tool: Callable[[str, dict], Awaitable[str]],
        on_text: Callable[[str], None],
        on_tool_start: Callable[[str, dict], None],
        on_tool_done: Callable[[str, str], None],
        on_turn_boundary: Callable[[], None] | None = None,
    ) -> dict:
        in_tok = out_tok = 0
        while True:
            final = await self._stream_once(messages, on_text)
            in_tok += final.usage.input_tokens or 0
            out_tok += final.usage.output_tokens or 0
            messages.append({"role": "assistant", "content": _assistant_blocks(final)})
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if final.stop_reason != "tool_use" or not tool_uses:
                break
            start_count = sum(tu.name == "start_workflow" for tu in tool_uses)
            results = []
            for tu in tool_uses:
                on_tool_start(tu.name, tu.input)
                if tu.name == "start_workflow" and start_count > 1:
                    out = ("error: ambiguous request produced multiple workflow starts; no workflow "
                           "was started. Ask the user which single workflow they want.")
                else:
                    out = await run_tool(tu.name, tu.input)
                on_tool_done(tu.name, out)
                results.append({"type": "tool_result", "tool_use_id": tu.id, "content": out})
            messages.append({"role": "user", "content": results})
            if on_turn_boundary:
                on_turn_boundary()
        in_rate, out_rate = _rate(self.model)
        cost = in_tok / 1e6 * in_rate + out_tok / 1e6 * out_rate
        return {"tokens": in_tok + out_tok, "cost": round(cost, 4)}
