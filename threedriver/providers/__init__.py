"""Provider clients: one interface, swappable AI backends.

A provider turns chat messages into a text reply. Messages are
``{"role": ..., "parts": [...]}`` where a part is either
``{"type": "text", "text": ...}`` or ``{"type": "image", "data": bytes,
"mime": "image/png"}``. Each client encodes images the way its API wants
them (data URL for OpenAI-compatible APIs, inline_data for Gemini).
"""

from __future__ import annotations

from .base import ProviderError, text_msg
from .gemini import GeminiProvider
from .openai_compat import OpenAICompatProvider
from .stub import StubProvider
from ..config import PROVIDERS


def get_provider(settings: dict):
    """Build the configured provider. Raises ProviderError if unusable."""
    provider_id = settings.get("provider", "stub")
    info = PROVIDERS.get(provider_id)
    if info is None:
        raise ProviderError(f"unknown provider: {provider_id}")
    client = info["client"]
    if client == "stub":
        return StubProvider()
    api_key = settings.get("api_key") or ""
    if not api_key:
        raise ProviderError(
            f"provider '{provider_id}' needs an API key — set it on the "
            "Settings page (or the matching environment variable)."
        )
    base_url = (settings.get("base_url") or "").rstrip("/")
    if client == "openai_compat":
        if not base_url:
            raise ProviderError(
                f"provider '{provider_id}' needs a base URL — set it on the "
                "Settings page."
            )
        return OpenAICompatProvider(
            api_key=api_key,
            base_url=base_url,
            json_mode_supported=bool(info.get("json_mode")),
        )
    if client == "gemini":
        return GeminiProvider(api_key=api_key, base_url=base_url)
    raise ProviderError(f"unsupported client type: {client}")


__all__ = [
    "ProviderError",
    "text_msg",
    "get_provider",
    "OpenAICompatProvider",
    "GeminiProvider",
    "StubProvider",
]
