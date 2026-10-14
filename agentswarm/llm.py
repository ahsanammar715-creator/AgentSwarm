"""Minimal OpenAI-compatible chat client with tool calling and cost accounting.

Works with OpenAI, DeepSeek, OpenRouter, Together, local vLLM/Ollama -- anything
that speaks POST {base_url}/chat/completions.
"""

from __future__ import annotations

import json
import random
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from .config import ModelConfig


class BudgetExceeded(RuntimeError):
    pass


class LLMError(RuntimeError):
    pass


@dataclass
class RoleUsage:
    calls: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    usd: float = 0.0


@dataclass
class CostLedger:
    budget_usd: float | None = None
    by_role: dict = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def record(self, role: str, cfg: ModelConfig, usage: dict) -> None:
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        details = usage.get("prompt_tokens_details") or {}
        cached = int(details.get("cached_tokens", 0) or usage.get("prompt_cache_hit_tokens", 0) or 0)
        uncached = max(prompt - cached, 0)
        usd = (
            uncached * cfg.input_price
            + cached * cfg.cached_input_price
            + completion * cfg.output_price
        ) / 1_000_000
        with self._lock:
            r = self.by_role.setdefault(role, RoleUsage())
            r.calls += 1
            r.input_tokens += prompt
            r.cached_tokens += cached
            r.output_tokens += completion
            r.usd += usd

    @property
    def total(self) -> float:
        with self._lock:
            return sum(r.usd for r in self.by_role.values())

    def check_budget(self) -> None:
        if self.budget_usd is not None and self.total >= self.budget_usd:
            raise BudgetExceeded(f"budget ${self.budget_usd:.2f} reached (spent ${self.total:.4f})")

    def line(self) -> str:
        with self._lock:
            parts = [f"{role} ${r.usd:.4f} ({r.calls} calls)" for role, r in sorted(self.by_role.items())]
            total = sum(r.usd for r in self.by_role.values())
        budget = f" / budget ${self.budget_usd:.2f}" if self.budget_usd else ""
        return f"cost: ${total:.4f}{budget} :: " + (", ".join(parts) or "no calls yet")

    def to_dict(self) -> dict:
        with self._lock:
            return {k: v.__dict__.copy() for k, v in self.by_role.items()}

    def load(self, data: dict) -> None:
        with self._lock:
            self.by_role = {k: RoleUsage(**v) for k, v in (data or {}).items()}


class LLM:
    """One model bound to one role, sharing a CostLedger with the rest of the swarm."""

    def __init__(self, cfg: ModelConfig, role: str, ledger: CostLedger, max_retries: int = 5):
        self.cfg = cfg
        self.role = role
        self.ledger = ledger
        self.max_retries = max_retries

    def chat(self, messages: list, tools: list | None = None, json_mode: bool = False) -> dict:
        """Returns the assistant message dict (role, content, tool_calls?)."""
        self.ledger.check_budget()
        if not self.cfg.api_key:
            raise LLMError(f"{self.role}: env var {self.cfg.api_key_env} is not set")
        body: dict = {"model": self.cfg.model, "messages": messages}
        if tools:
            body["tools"] = tools
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.cfg.reasoning_effort:
            body["reasoning_effort"] = self.cfg.reasoning_effort
        if self.cfg.max_tokens:
            body["max_completion_tokens"] = self.cfg.max_tokens
        data = self._post(body)
        self.ledger.record(self.role, self.cfg, data.get("usage") or {})
        choices = data.get("choices") or []
        if not choices:
            raise LLMError(f"{self.role}: empty response: {str(data)[:300]}")
        msg = choices[0].get("message") or {}
        msg.setdefault("role", "assistant")
        msg.pop("reasoning_content", None)  # DeepSeek: must not be echoed back
        return msg

    def _post(self, body: dict) -> dict:
        url = self.cfg.base_url.rstrip("/") + "/chat/completions"
        payload = json.dumps(body).encode()
        last_err: Exception | None = None
        for attempt in range(self.max_retries):
            req = urllib.request.Request(
                url,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.cfg.api_key}",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=self.cfg.timeout) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as e:
                detail = e.read().decode(errors="replace")[:500]
                last_err = LLMError(f"{self.role}: HTTP {e.code}: {detail}")
                if e.code not in (408, 409, 429, 500, 502, 503, 504):
                    raise last_err from e
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                last_err = LLMError(f"{self.role}: {e}")
            time.sleep(min(2 ** attempt + random.random(), 30))
        raise last_err or LLMError("unknown error")

    def ask_json(self, system: str, user: str) -> dict:
        """Single-shot call that must return a JSON object."""
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        for _ in range(3):
            msg = self.chat(messages, json_mode=True)
            text = msg.get("content") or ""
            try:
                return parse_json(text)
            except ValueError:
                messages += [msg, {"role": "user", "content": "That was not valid JSON. Reply with one JSON object only."}]
        raise LLMError(f"{self.role}: could not get valid JSON")


def parse_json(text: str) -> dict:
    text = text.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text
        text = text.rsplit("```", 1)[0]
    try:
        value = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise ValueError("no JSON object found")
        value = json.loads(text[start : end + 1])
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value
