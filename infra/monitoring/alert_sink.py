"""本地告警接收端：打印 Alertmanager webhook 内容，用于验证「告警 → 通知」链路。

仅开发验证用；生产应换成飞书/企微/PagerDuty 等真实接收端。
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer


class Handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        try:
            for alert in json.loads(raw).get("alerts", []):
                labels = alert.get("labels", {})
                summary = alert.get("annotations", {}).get("summary", "")
                print(
                    f"[ALERT] {alert.get('status', '?'):<8} "
                    f"{labels.get('alertname', '?'):<24} "
                    f"severity={labels.get('severity', '-'):<8} {summary}",
                    flush=True,
                )
        except Exception:
            print(f"[ALERT] raw: {raw.decode('utf-8', 'ignore')[:500]}", flush=True)
        self.send_response(200)
        self.end_headers()

    def log_message(self, *args) -> None:  # 静默访问日志
        pass


if __name__ == "__main__":
    print("[alert-sink] listening on :9000", flush=True)
    HTTPServer(("0.0.0.0", 9000), Handler).serve_forever()
