"""Minimal HTTP application entry point."""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.market_data import history, market_overview
from core.signals import drawdown_rows, zones

ROOT = Path(__file__).parent


class DashboardHandler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/overview":
                payload = drawdown_rows(market_overview())
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            elif parsed.path == "/api/analysis":
                coin_id = parse_qs(parsed.query).get("coin", ["bitcoin"])[0]
                payload = zones(history(coin_id))
                self._send(200, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")
            else:
                page = (ROOT / "dashboard" / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")
        except Exception as exc:
            self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"高位回撤观测智能体：http://127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler).serve_forever()
