"""Chat-model clients used by consolidation (and later reflection)."""

from __future__ import annotations

import json
import os
import re
import urllib.parse
import urllib.request
from typing import Protocol

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class LLM(Protocol):
    """Anything that turns (system, user) into text. Adapters can wrap their framework's own model."""

    model_id: str

    def complete(self, system: str, user: str) -> str: ...


class OpenAICompatibleChat:
    """POST {base_url}/chat/completions (DeepSeek, OpenAI, Ollama, vLLM, ...)."""

    def __init__(self, base_url: str, model: str, api_key: str | None = None, *,
                 temperature: float = 0.2, json_mode: bool = True, timeout: float = 120.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.model_id = model
        self._api_key = api_key
        self._temperature = temperature
        self._json_mode = json_mode
        self._timeout = timeout
        host = urllib.parse.urlparse(self.base_url).hostname or ""
        handlers = [urllib.request.ProxyHandler({})] if host in _LOCAL_HOSTS else []
        self._opener = urllib.request.build_opener(*handlers)

    def complete(self, system: str, user: str) -> str:
        body: dict = {
            "model": self.model,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
            "temperature": self._temperature,
        }
        if self._json_mode:
            body["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        req = urllib.request.Request(self.base_url + "/chat/completions",
                                     data=json.dumps(body).encode("utf-8"), headers=headers, method="POST")
        with self._opener.open(req, timeout=self._timeout) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"] or ""


def llm_from_config(cfg: dict) -> LLM | None:
    """Build a client from the [llm] table of config.toml. Prefer `api_key_env` over a key in the file."""
    if not cfg:
        return None
    key = cfg.get("api_key")
    if cfg.get("api_key_env"):
        key = os.environ.get(cfg["api_key_env"]) or key
    return OpenAICompatibleChat(cfg["base_url"], cfg["model"], key,
                                temperature=float(cfg.get("temperature", 0.2)),
                                json_mode=bool(cfg.get("json_mode", True)),
                                timeout=float(cfg.get("timeout", 120)))


def parse_json(text: str) -> dict:
    """Parse a model's JSON answer, tolerating ```json fences."""
    match = _FENCE.match(text)
    if match:
        text = match.group(1)
    data = json.loads(text)
    if not isinstance(data, dict):
        raise ValueError("expected a JSON object")
    return data
