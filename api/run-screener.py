import csv
import io
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

from blob_store import upload_csv
from kite_web_auth import get_kite_session_from_token
from screener import run_screener
from token_store import load_access_token


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def _handle(self):
        try:
            token = load_access_token()
        except RuntimeError as e:
            self._json(500, {"error": f"Token store not configured: {e}"})
            return

        if not token:
            self._json(401, {"error": "Not logged in. Log in with Zerodha first."})
            return

        try:
            kite = get_kite_session_from_token(token)
            result = run_screener(kite)
        except (FileNotFoundError, ValueError, RuntimeError) as e:
            self._json(500, {"error": str(e)})
            return

        result["csv_url"] = self._store_csv_snapshot(result["top_rows"])
        self._json(200, result)

    def _store_csv_snapshot(self, rows: list[dict]):
        if not rows:
            return None
        try:
            buf = io.StringIO()
            writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)
            pathname = f"atrx/atrx_{datetime.now():%Y%m%d_%H%M}.csv"
            return upload_csv(pathname, buf.getvalue().encode())
        except RuntimeError:
            # Blob storage not configured -- results still return to the UI.
            return None

    def _json(self, status: int, payload: dict):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())
