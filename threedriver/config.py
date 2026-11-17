"""Settings: provider choice, API keys, Blender location, timeouts.

Settings live in ONE local file (``settings.json`` in the working directory
by default, overridable with THREEDRIVER_SETTINGS). The web UI edits it, so
changing provider/key/model never means editing code. The file is gitignored
and written with mode 0600 on POSIX. Environment variables override the file.

No key is ever printed, logged, or committed. ``redact()`` masks keys for
display; the API only ever returns the redacted form.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Registry: provider id -> wiring facts. "client" selects the client class,
# "json_mode" says whether response_format: json_object may be sent (some
# routers reject it, so it defaults off for 9router / generic endpoints).
PROVIDERS: dict[str, dict] = {
    "stub": {
        "label": "Stub (demo, tanpa API key — selalu membuat plat contoh)",
        "client": "stub",
        "default_base_url": "",
        "default_model": "stub",
        "key_env": "",
        "json_mode": False,
    },
    "nine_router": {
        "label": "9router (OpenAI-compatible, 1 key routing banyak model)",
        "client": "openai_compat",
        "default_base_url": "",
        "default_model": "HERMES2",
        "key_env": "NINE_ROUTER_API_KEY",
        "json_mode": False,
    },
    "deepseek": {
        "label": "DeepSeek",
        "client": "openai_compat",
        "default_base_url": "https://api.deepseek.com/v1",
        "default_model": "deepseek-chat",
        "key_env": "DEEPSEEK_API_KEY",
        "json_mode": True,
    },
    "gemini": {
        "label": "Gemini (Google AI Studio)",
        "client": "gemini",
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.5-flash",
        "key_env": "GEMINI_API_KEY",
        "json_mode": True,
    },
    "openai_compat": {
        "label": "OpenAI-compatible generik (isi sendiri base URL + model)",
        "client": "openai_compat",
        "default_base_url": "",
        "default_model": "",
        "key_env": "",
        "json_mode": False,
    },
}

DEFAULTS: dict = {
    "provider": "stub",
    "model": "",
    "base_url": "",
    "api_key": "",
    "blender": "",
    "timeout": 600,
    "jobs_root": "",
}

ENV_OVERRIDES = {
    "provider": "THREEDRIVER_PROVIDER",
    "model": "THREEDRIVER_MODEL",
    "base_url": "THREEDRIVER_BASE_URL",
    "api_key": "THREEDRIVER_API_KEY",
    "blender": "THREEDRIVER_BLENDER",
    "timeout": "THREEDRIVER_TIMEOUT",
    "jobs_root": "THREEDRIVER_JOBS_ROOT",
}


def default_settings_path() -> Path:
    env = os.environ.get("THREEDRIVER_SETTINGS")
    return Path(env) if env else Path.cwd() / "settings.json"


def _provider_defaults(provider_id: str) -> dict:
    return PROVIDERS.get(provider_id, PROVIDERS["stub"])


def load_settings(path: str | Path | None = None) -> dict:
    """Defaults <- settings file <- provider key env <- THREEDRIVER_* env."""
    path = Path(path) if path else default_settings_path()
    settings = dict(DEFAULTS)
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                settings.update({k: v for k, v in stored.items() if k in DEFAULTS})
        except (json.JSONDecodeError, OSError):
            pass
    provider = _provider_defaults(settings["provider"])
    key_env = provider.get("key_env")
    if key_env and os.environ.get(key_env):
        settings["api_key"] = os.environ[key_env]
    for key, env_name in ENV_OVERRIDES.items():
        if os.environ.get(env_name):
            value: object = os.environ[env_name]
            if key == "timeout":
                try:
                    value = int(value)  # type: ignore[arg-type]
                except ValueError:
                    continue
            settings[key] = value
    # Fill empty model/base_url from the selected provider's defaults.
    if not settings["model"]:
        settings["model"] = provider.get("default_model", "")
    if not settings["base_url"]:
        settings["base_url"] = provider.get("default_base_url", "")
    settings["_path"] = str(path)
    return settings


def save_settings(updates: dict, path: str | Path | None = None) -> dict:
    """Merge updates into the settings file. Empty api_key keeps the old one."""
    path = Path(path) if path else default_settings_path()
    current: dict = {}
    if path.is_file():
        try:
            stored = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(stored, dict):
                current = stored
        except (json.JSONDecodeError, OSError):
            pass
    for key, value in updates.items():
        if key not in DEFAULTS:
            continue
        if key == "api_key" and value in ("", None):
            continue  # blank key field in the UI means "keep existing"
        current[key] = value
    path.write_text(json.dumps(current, indent=2), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass  # Windows: no POSIX permissions; file stays local + gitignored
    return load_settings(path)


def redact(settings: dict) -> dict:
    """Copy safe to show in the UI / API: the key is masked, never shown."""
    out = {k: v for k, v in settings.items() if not k.startswith("_")}
    key = out.get("api_key") or ""
    out["api_key"] = f"••••{key[-4:]}" if len(key) > 4 else ("••••" if key else "")
    out["has_api_key"] = bool(key)
    return out


def providers_info() -> list[dict]:
    """Public provider list for the settings UI (no secrets)."""
    return [
        {"id": pid, "label": info["label"],
         "default_base_url": info["default_base_url"],
         "default_model": info["default_model"]}
        for pid, info in PROVIDERS.items()
    ]
