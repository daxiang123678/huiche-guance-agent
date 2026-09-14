"""极简 HTTP 入口与接口路由。

数据来源说明（页面会如实展示实际用到的那一层）：
    币安官方公开行情接口优先，失败逐级降级到综合平台数据源。
    实际来源由 core.market_data.LAST_SOURCE 记录。
"""
from __future__ import annotations

import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from core.market_data import LAST_SOURCE, history, market_overview, source_label
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

    def _json(self, payload: object) -> None:
        self._send(200, json.dumps(payload, ensure_ascii=False).encode(),
                   "application/json; charset=utf-8")

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/api/overview":
                # 回撤榜：数据层取数 + 自研引擎排序
                rows = drawdown_rows(market_overview())
                key = LAST_SOURCE.get("overview", "")
                label = source_label(key)
                for row in rows:
                    row["dataSource"] = label
                self._json({"rows": rows, "dataSource": label, "dataSourceKey": key})

            elif parsed.path == "/api/analysis":
                # 单币成交量分布与支撑压力
                coin = parse_qs(parsed.query).get("coin", ["BTCUSDT"])[0]
                payload = zones(history(coin))
                key = LAST_SOURCE.get("history", "")
                payload["dataSource"] = source_label(key)
                payload["dataSourceKey"] = key
                payload["coin"] = coin
                self._json(payload)

            elif parsed.path == "/api/health":
                self._json({"ok": True, "sources": dict(LAST_SOURCE)})

            else:
                page = (ROOT / "dashboard" / "index.html").read_bytes()
                self._send(200, page, "text/html; charset=utf-8")

        except Exception as exc:  # noqa: BLE001
            self._send(502, json.dumps({"error": str(exc)}, ensure_ascii=False).encode(),
                       "application/json; charset=utf-8")

    def log_message(self, format: str, *args) -> None:
        return


if __name__ == "__main__":
    port = int(os.environ.get("PORT", "8787"))
    print(f"高位回撤观测智能体：http://127.0.0.1:{port}")
    ThreadingHTTPServer(("0.0.0.0", port), DashboardHandler).serve_forever()
