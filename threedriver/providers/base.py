"""Shared provider types."""

from __future__ import annotations

from typing import Protocol


class ProviderError(RuntimeError):
    """Anything wrong talking to an AI provider (auth, HTTP, bad payload)."""


def text_msg(role: str, text: str) -> dict:
    return {"role": role, "parts": [{"type": "text", "text": text}]}


def image_msg(role: str, text: str, images: list[tuple[bytes, str]]) -> dict:
    parts: list[dict] = [{"type": "text", "text": text}]
    for data, mime in images:
        parts.append({"type": "image", "data": data, "mime": mime})
    return {"role": role, "parts": parts}


class Provider(Protocol):
    def complete(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        """Return the model's reply text. Raises ProviderError on failure."""
        ...
