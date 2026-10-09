"""Two small LLM clients: local Ollama, or any OpenAI-compatible endpoint (e.g. Gemini, Groq)."""

import os
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx

from jobagent.config import LLMConfig

TIMEOUT = 120.0


class LLMError(Exception):
    """The model call failed. Never contains prompt or response text."""


class LLMClient(Protocol):
    host: str
    is_local: bool

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str: ...


def _post(url: str, payload: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=TIMEOUT)
    except httpx.HTTPError as exc:
        raise LLMError(f"request failed: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise LLMError(f"model endpoint returned HTTP {resp.status_code}")
    try:
        data = resp.json()
    except ValueError as exc:
        raise LLMError("model endpoint returned invalid JSON") from exc
    if not isinstance(data, dict):
        raise LLMError("unexpected response shape")
    return data


class OllamaClient:
    is_local = True

    def __init__(self, cfg: LLMConfig) -> None:
        self.base = cfg.base_url.rstrip("/")
        self.model = cfg.model
        self.host = urlsplit(self.base).netloc

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        data = _post(
            f"{self.base}/api/chat",
            {
                "model": self.model,
                "stream": False,
                "format": schema,
                "options": {"temperature": 0},
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {},
        )
        try:
            return str(data["message"]["content"])
        except (KeyError, TypeError) as exc:
            raise LLMError("unexpected response shape") from exc


class OpenAICompatClient:
    def __init__(self, cfg: LLMConfig, api_key: str) -> None:
        self.base = cfg.base_url.rstrip("/")
        self.model = cfg.model
        self.api_key = api_key
        parts = urlsplit(self.base)
        self.host = parts.netloc
        self.is_local = parts.hostname in {"localhost", "127.0.0.1", "::1"}

    def complete(self, system: str, user: str, schema: dict[str, Any]) -> str:
        data = _post(
            f"{self.base}/chat/completions",
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            },
            {"Authorization": f"Bearer {self.api_key}"},
        )
        try:
            return str(data["choices"][0]["message"]["content"])
        except (KeyError, IndexError, TypeError) as exc:
            raise LLMError("unexpected response shape") from exc


def make_client(cfg: LLMConfig) -> LLMClient | None:
    if cfg.provider == "none":
        return None
    if cfg.provider == "ollama":
        return OllamaClient(cfg)
    key = os.environ.get("LLM_API_KEY", "").strip()
    if not key:
        raise LLMError("LLM_API_KEY is not set (put it in .env)")
    return OpenAICompatClient(cfg, key)
