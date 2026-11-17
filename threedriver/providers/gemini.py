"""Gemini client via the Google AI Studio REST API (generateContent).

Uses ``{base_url}/models/{model}:generateContent`` with the API key as a
query parameter (the standard AI Studio auth). Images are sent as
``inline_data`` parts. A Google AI Pro / Gemini app subscription does NOT
grant API access — this needs a separate AI Studio API key.
"""

from __future__ import annotations

import base64

import httpx

from .base import ProviderError


class GeminiProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://generativelanguage.googleapis.com/v1beta",
        timeout: float = 180.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout

    def _encode(self, messages: list[dict]) -> tuple[dict | None, list[dict]]:
        system_instruction = None
        contents: list[dict] = []
        for msg in messages:
            parts = []
            for part in msg["parts"]:
                if part["type"] == "text":
                    parts.append({"text": part["text"]})
                elif part["type"] == "image":
                    parts.append({
                        "inline_data": {
                            "mime_type": part["mime"],
                            "data": base64.b64encode(part["data"]).decode("ascii"),
                        }
                    })
            if msg["role"] == "system":
                system_instruction = {"parts": parts}
            else:
                role = "model" if msg["role"] == "assistant" else "user"
                contents.append({"role": role, "parts": parts})
        return system_instruction, contents

    def complete(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        system_instruction, contents = self._encode(messages)
        payload: dict = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_tokens,
            },
        }
        if system_instruction:
            payload["systemInstruction"] = system_instruction
        if json_mode:
            payload["generationConfig"]["responseMimeType"] = "application/json"
        try:
            # trust_env=False: see openai_compat.py (malformed proxy env vars
            # must not crash the client before any request is made).
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                resp = client.post(
                    f"{self._base_url}/models/{model}:generateContent",
                    params={"key": self._api_key},
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach Gemini: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"Gemini HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise ProviderError(
                f"Gemini returned non-JSON: {resp.text[:200]!r}"
            ) from exc
        if isinstance(data.get("error"), dict):
            raise ProviderError(
                f"Gemini error: {data['error'].get('message', data['error'])}"
            )
        texts: list[str] = []
        for cand in data.get("candidates", []) or []:
            for part in (cand.get("content") or {}).get("parts", []) or []:
                if isinstance(part.get("text"), str):
                    texts.append(part["text"])
        joined = "".join(texts).strip()
        if not joined:
            raise ProviderError(
                "Gemini returned no text (the reply may have been blocked "
                "by a safety filter or the model name may be wrong)."
            )
        return joined
