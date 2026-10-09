"""The model client: one HTTP call to a local Ollama server (D12).

`LLMClient` is the whole interface the write-up needs: text in, text out. Tests pass a fake, and
CI never constructs a real client, so CI never calls Ollama (D9, D12).

`OllamaClient` asks for deterministic, schema-shaped output: temperature 0 (always the most likely
next token), a fixed seed (42), and the JSON Schema itself as Ollama's `format`, so the server
constrains the model to that shape. A model that still misbehaves is caught by the validation in
writeup.py. Errors are not handled here: an httpx timeout or transport error, an HTTP error status,
or a reply without `message.content` (a ValueError) is raised, and `write` maps it to a fallback.
"""

from typing import Protocol

import httpx


class LLMClient(Protocol):
    def generate(self, prompt: str, schema: dict) -> str: ...


class OllamaClient:
    def __init__(
        self,
        base_url: str,
        model: str,
        timeout_s: float = 60.0,
        seed: int = 42,
        think: bool | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._timeout_s = timeout_s
        self._seed = seed
        self._think = think
        self._transport = transport

    def generate(self, prompt: str, schema: dict) -> str:
        """POST /api/chat {model, messages:[{role:user, content:prompt}], stream:false,
        format:schema, options:{temperature:0, seed:42}} (+ "think": false only when think is
        False, for thinking models). Returns message.content."""
        body: dict = {
            "model": self._model,
            "messages": [{"role": "user", "content": prompt}],
            "stream": False,
            "format": schema,
            "options": {"temperature": 0, "seed": self._seed},
        }
        if self._think is False:
            body["think"] = False
        with httpx.Client(timeout=self._timeout_s, transport=self._transport) as client:
            r = client.post(f"{self._base_url}/api/chat", json=body)
            r.raise_for_status()
        try:
            content = r.json()["message"]["content"]
        except (ValueError, KeyError, TypeError) as e:
            raise ValueError("the Ollama reply has no message.content") from e
        if not isinstance(content, str):
            raise ValueError("the Ollama message.content is not text")
        return content
