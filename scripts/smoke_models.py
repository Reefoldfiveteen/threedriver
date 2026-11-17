#!/usr/bin/env python3
"""Smoke-test the model-list fetch against local mock provider servers.

Covers: OpenAI-compatible /v1/models parsing (dedupe + sort + Bearer
auth), empty-body and HTTP-error failure modes (no key leakage in
errors), Gemini /v1beta/models parsing (generateContent filter, name
prefix strip, pagination), and the POST /api/models web endpoint
(stub, mock 9router-style provider, unreachable provider) plus the new
Settings UI elements. No Blender, no network beyond localhost.

Usage:  python scripts/smoke_models.py
Exit 0 = all checks passed.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from threedriver.providers import ProviderError  # noqa: E402
from threedriver.providers.gemini import GeminiProvider  # noqa: E402
from threedriver.providers.openai_compat import OpenAICompatProvider  # noqa: E402

PORT = 18768
BASE = f"http://127.0.0.1:{PORT}"
TEST_KEY = "test-key-12345"


class MockHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the test output clean
        pass

    def _json(self, status: int, obj) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)
        if path == "/v1/models":
            if self.headers.get("Authorization") != f"Bearer {TEST_KEY}":
                self._json(401, {"error": {"message": "bad key"}})
                return
            self._json(200, {"object": "list", "data": [
                {"id": "zeta-model"}, {"id": "alpha-model"},
                {"id": "alpha-model"}, {"id": "HERMES2"}]})
        elif path == "/v1-empty/models":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif path == "/v1-err/models":
            self._json(403, {"error": {"message": "forbidden"}})
        elif path == "/v1beta/models":
            if query.get("key") != [TEST_KEY]:
                self._json(400, {"error": {"message": "API key not valid"}})
                return
            if query.get("pageToken") == ["p2"]:
                self._json(200, {"models": [
                    {"name": "models/gemini-1.5-pro",
                     "supportedGenerationMethods": ["generateContent"]}]})
            else:
                self._json(200, {
                    "models": [
                        {"name": "models/gemini-2.0-flash",
                         "supportedGenerationMethods": ["generateContent"]},
                        {"name": "models/text-embedding-004",
                         "supportedGenerationMethods": ["embedContent"]}],
                    "nextPageToken": "p2"})
        else:
            self._json(404, {"error": {"message": "not found"}})


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    mock = f"http://127.0.0.1:{server.server_address[1]}"
    web = None
    try:
        # --- OpenAI-compatible client, direct ---
        oc = OpenAICompatProvider(api_key=TEST_KEY, base_url=f"{mock}/v1")
        models = oc.list_models()
        assert models == ["HERMES2", "alpha-model", "zeta-model"], models
        print("openai /v1/models ok (sorted, deduped):", models)

        bad = OpenAICompatProvider(api_key="wrong", base_url=f"{mock}/v1")
        try:
            bad.list_models()
        except ProviderError as exc:
            assert "401" in str(exc), exc
            assert "wrong" not in str(exc), "key leaked into error"
            print("openai 401 -> clean ProviderError, no key leak")
        else:
            raise AssertionError("401 did not raise")

        empty = OpenAICompatProvider(api_key=TEST_KEY,
                                     base_url=f"{mock}/v1-empty")
        try:
            empty.list_models()
        except ProviderError as exc:
            assert "empty body" in str(exc), exc
            print("openai empty 200 body -> ProviderError")
        else:
            raise AssertionError("empty body did not raise")

        dead = OpenAICompatProvider(api_key=TEST_KEY,
                                    base_url="http://127.0.0.1:9/v1")
        try:
            dead.list_models()
        except ProviderError as exc:
            assert "could not reach" in str(exc), exc
            print("unreachable provider -> ProviderError")
        else:
            raise AssertionError("unreachable did not raise")

        # --- Gemini client, direct ---
        gem = GeminiProvider(api_key=TEST_KEY, base_url=f"{mock}/v1beta")
        models = gem.list_models()
        assert models == ["gemini-1.5-pro", "gemini-2.0-flash"], models
        print("gemini /v1beta/models ok (paged, filtered, stripped):", models)

        # --- web endpoint ---
        settings_path = ROOT / "tmp" / "smoke-models-settings.json"
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        if settings_path.exists():
            settings_path.unlink()
        env = dict(os.environ, THREEDRIVER_SETTINGS=str(settings_path))
        web = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "serve.py"),
             "--port", str(PORT)],
            cwd=ROOT, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        import httpx
        with httpx.Client(timeout=15, trust_env=False) as client:
            deadline = time.monotonic() + 30
            while True:
                try:
                    if client.get(f"{BASE}/api/health").json().get("ok"):
                        break
                except httpx.RequestError:
                    pass
                if time.monotonic() > deadline:
                    print("FAIL: server did not come up")
                    return 1
                time.sleep(0.5)
            print("server up")

            page = client.get(f"{BASE}/").text
            assert "fetchModelsBtn" in page and "modelList" in page, \
                "new Settings UI elements missing"
            print("Settings UI has fetch-models button + dropdown")

            stub = client.post(f"{BASE}/api/models",
                               json={"provider": "stub"}).json()
            assert stub == {"ok": True, "provider": "stub",
                            "models": ["stub"], "count": 1}, stub
            print("endpoint stub ok:", stub)

            nine = client.post(f"{BASE}/api/models", json={
                "provider": "nine_router", "base_url": f"{mock}/v1",
                "api_key": TEST_KEY}).json()
            assert nine["ok"] and nine["models"] == [
                "HERMES2", "alpha-model", "zeta-model"], nine
            print("endpoint mock 9router ok:", nine["models"])

            nokey = client.post(f"{BASE}/api/models", json={
                "provider": "nine_router",
                "base_url": f"{mock}/v1"}).json()
            assert not nokey["ok"] and "API key" in nokey["error"], nokey
            print("endpoint missing key -> ok:false:", nokey["error"][:60])

            down = client.post(f"{BASE}/api/models", json={
                "provider": "deepseek", "base_url": "http://127.0.0.1:9/v1",
                "api_key": "x"}).json()
            assert not down["ok"] and "could not reach" in down["error"], down
            print("endpoint unreachable -> ok:false")
        print("MODELS SMOKE PASSED")
        return 0
    except AssertionError as exc:
        print("FAIL:", exc)
        return 1
    finally:
        server.shutdown()
        if web is not None:
            web.terminate()
            try:
                web.wait(timeout=10)
            except subprocess.TimeoutExpired:
                web.kill()


if __name__ == "__main__":
    raise SystemExit(main())
