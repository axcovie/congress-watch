"""Optional webhook receiver so results arrive in real time instead of by polling.

Run:  congress-watch webhook --port 8787
Then expose it over HTTPS (e.g. a reverse proxy or tunnel) and run
`congress-watch setup --webhook-url https://your-host/telem`.

Signatures are verified with the official `telem-sdk` helper (`pip install telem-sdk`).
"""
from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, HTTPServer

from .store import Store
from .telem_client import extract_results


def serve(db_path: str, port: int = 8787) -> None:
    try:
        from telem import verify_webhook
    except ImportError as exc:
        raise SystemExit("The webhook receiver needs the Telem SDK: pip install telem-sdk") from exc

    store = Store(db_path)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(length)
            monitor_id = self.headers.get("Telem-Monitor-Id", "")
            row = store.monitor_by_id(monitor_id)
            if row is None or not row["webhook_secret"]:
                self.send_response(404)
                self.end_headers()
                return
            sig = self.headers.get("Telem-Signature", "")
            if not verify_webhook(body, sig, row["webhook_secret"]):
                self.send_response(401)
                self.end_headers()
                return
            payload = json.loads(body or b"{}")
            added = store.add_results(row["topic_key"], extract_results(payload))
            print(f"[webhook] {row['topic_key']}: +{added} new")
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *args):  # keep output quiet
            pass

    print(f"Listening for Telem webhooks on :{port}")
    HTTPServer(("0.0.0.0", port), Handler).serve_forever()
