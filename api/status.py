import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

from token_store import load_access_token


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            token = load_access_token()
        except RuntimeError:
            token = None

        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps({"logged_in": bool(token)}).encode())
