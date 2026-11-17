"""OpenAI-compatible chat completions client (DeepSeek, 9router, generic).

POSTs to ``{base_url}/chat/completions``. Response parsing is deliberately
tolerant, because routers in the wild return more than clean JSON:

* plain JSON chat-completion objects,
* SSE streams (``data: {...}`` lines + ``data: [DONE]``) — some routers
  append an SSE terminator even to non-streaming replies, which breaks
  strict ``response.json()`` parsing,
* concatenated JSON objects / delta chunks (streamed parts),
* HTTP 200 with an EMPTY body (observed when a router combo's first model
  fails instead of falling back) — treated as an error, not a success.

Never logs or includes the API key in errors.
"""

from __future__ import annotations

import base64
import json

import httpx

from .base import ProviderError


def _content_from_completion(obj: dict) -> str:
    if not isinstance(obj, dict):
        return ""
    if isinstance(obj.get("error"), dict):
        raise ProviderError(
            f"provider error: {obj['error'].get('message', obj['error'])}"
        )
    texts: list[str] = []
    for choice in obj.get("choices", []) or []:
        message = choice.get("message") or {}
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    texts.append(part.get("text", ""))
        delta = choice.get("delta") or {}
        if isinstance(delta.get("content"), str):
            texts.append(delta["content"])
    return "".join(texts)


def _parse_body(text: str) -> str:
    stripped = text.strip()
    if not stripped:
        raise ProviderError(
            "provider returned an empty body (HTTP 200, no content). "
            "The selected model/combo may have failed server-side without "
            "falling back — try another model."
        )
    try:
        data = json.loads(stripped)
        content = _content_from_completion(data)
        if content.strip():
            return content
    except json.JSONDecodeError:
        pass
    # SSE lines and/or concatenated JSON objects: parse each piece.
    pieces: list[str] = []
    for line in stripped.splitlines():
        line = line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
            if line in ("", "[DONE]"):
                continue
        if line.startswith("{"):
            pieces.append(line)
    if not pieces:
        # Last resort: brace-scan for the first complete JSON object.
        start = stripped.find("{")
        if start >= 0:
            depth = 0
            for i in range(start, len(stripped)):
                if stripped[i] == "{":
                    depth += 1
                elif stripped[i] == "}":
                    depth -= 1
                    if depth == 0:
                        pieces.append(stripped[start:i + 1])
                        break
    texts: list[str] = []
    for piece in pieces:
        try:
            obj = json.loads(piece)
        except json.JSONDecodeError:
            continue
        try:
            texts.append(_content_from_completion(obj))
        except ProviderError:
            raise
    joined = "".join(texts).strip()
    if joined:
        return joined
    raise ProviderError(
        "could not parse the provider response as a chat completion "
        f"(first 200 chars: {stripped[:200]!r})."
    )


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        json_mode_supported: bool = False,
        timeout: float = 180.0,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._json_mode_supported = json_mode_supported
        self._timeout = timeout

    def _encode_messages(self, messages: list[dict]) -> list[dict]:
        encoded = []
        for msg in messages:
            parts = []
            for part in msg["parts"]:
                if part["type"] == "text":
                    parts.append({"type": "text", "text": part["text"]})
                elif part["type"] == "image":
                    b64 = base64.b64encode(part["data"]).decode("ascii")
                    parts.append({
                        "type": "image_url",
                        "image_url": {"url": f"data:{part['mime']};base64,{b64}"},
                    })
            if len(parts) == 1 and parts[0]["type"] == "text":
                encoded.append({"role": msg["role"], "content": parts[0]["text"]})
            else:
                encoded.append({"role": msg["role"], "content": parts})
        return encoded

    def complete(
        self,
        messages: list[dict],
        *,
        model: str,
        temperature: float = 0.2,
        max_tokens: int = 4096,
        json_mode: bool = False,
    ) -> str:
        payload: dict = {
            "model": model,
            "messages": self._encode_messages(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        if json_mode and self._json_mode_supported:
            payload["response_format"] = {"type": "json_object"}
        try:
            # trust_env=False: ignore ambient proxy env vars, which may be
            # malformed (e.g. bare IPv6 entries in no_proxy) and crash
            # httpx URL parsing before any request is made.
            with httpx.Client(timeout=self._timeout, trust_env=False) as client:
                resp = client.post(
                    f"{self._base_url}/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach provider: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"provider HTTP {resp.status_code}: {resp.text[:300]}"
            )
        return _parse_body(resp.text)

    def list_models(self) -> list[str]:
        """GET {base_url}/models -> sorted unique model ids.

        Tolerant of the shapes routers return: {"data": [{"id": ...}]},
        {"models": [...]}, or a bare list. An empty 200 body is an error
        (see the module docstring: routers return it when a combo fails
        without falling back), never an empty list.
        """
        try:
            with httpx.Client(timeout=20.0, trust_env=False) as client:
                resp = client.get(
                    f"{self._base_url}/models",
                    headers={"Authorization": f"Bearer {self._api_key}"},
                )
        except httpx.RequestError as exc:
            raise ProviderError(f"could not reach provider: {exc}") from exc
        if resp.status_code >= 400:
            raise ProviderError(
                f"provider HTTP {resp.status_code}: {resp.text[:300]}"
            )
        text = resp.text.strip()
        if not text:
            raise ProviderError(
                "provider returned an empty body for the model list "
                "(HTTP 200, no content) — try again, or another base URL."
            )
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            start, end = text.find("{"), text.rfind("}")
            if start < 0 or end <= start:
                raise ProviderError(
                    "model list was not JSON "
                    f"(first 200 chars: {text[:200]!r})."
                )
            data = json.loads(text[start:end + 1])
        entries = []
        if isinstance(data, dict):
            entries = data.get("data") or data.get("models") or []
        elif isinstance(data, list):
            entries = data
        ids: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict) and isinstance(entry.get("id"), str):
                ids.add(entry["id"])
            elif isinstance(entry, str):
                ids.add(entry)
            elif isinstance(entry, dict) and isinstance(entry.get("name"), str):
                ids.add(entry["name"])
        return sorted(ids)
