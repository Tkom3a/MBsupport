#!/usr/bin/env python3
"""Учебный стенд: виртуальное окно клиента MT + кликающий агент.

Не трогает живой MoonTrader.exe и algorithms.config.
Открыть: python serve.py
Страница: http://127.0.0.1:4870/
"""
from __future__ import annotations

import json
import os
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
LAUNCHER = ROOT.parent
PORT = 4870


def load_env() -> None:
    path = LAUNCHER / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip().strip('"').strip("'")


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path in {"/proxy/mt-plan", "/api/mt-plan"}:
            self._proxy_plan(parsed.query)
            return
        super().do_GET()

    def _proxy_plan(self, query: str) -> None:
        qs = parse_qs(query)
        base = (qs.get("shotcore") or [os.getenv("SHOTCORE_URL", "")])[0].rstrip("/")
        token = (qs.get("token") or [os.getenv("SHOTCORE_TOKEN", "")])[0]
        lookback = (qs.get("lookback") or [os.getenv("LOOKBACK_MIN", "180")])[0]
        if not base:
            self._json(400, {"error": "Задайте ShotCore URL или SHOTCORE_URL в mt_launcher/.env"})
            return
        url = f"{base}/api/mt-plan?lookback={lookback}"
        headers = {"User-Agent": "ShotCore-virtual-client"}
        if token:
            headers["X-Shot-Token"] = token
            url += f"&token={token}"
        try:
            req = Request(url, headers=headers)
            with urlopen(req, timeout=12) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:400]
            self._json(exc.code, {"error": f"ShotCore {exc.code}: {body}"})
            return
        except (URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._json(502, {"error": str(exc)})
            return
        self._json(200, payload)

    def _json(self, status: int, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


def main() -> None:
    load_env()
    os.chdir(ROOT)
    httpd = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"Виртуальное окно клиента: http://127.0.0.1:{PORT}/")
    print("Это учебный стенд. Живой MoonTrader не открывается и не кликается.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nостановлен")


if __name__ == "__main__":
    main()
