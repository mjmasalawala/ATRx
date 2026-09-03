import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

from kite_web_auth import exchange_request_token
from token_store import save_access_token


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        query = parse_qs(urlparse(self.path).query)
        status = (query.get("status") or [None])[0]
        request_token = (query.get("request_token") or [None])[0]

        if status != "success" or not request_token:
            self._redirect("/?login=failed")
            return

        try:
            access_token = exchange_request_token(request_token)
            save_access_token(access_token)
        except RuntimeError:
            self._redirect("/?login=failed")
            return

        self._redirect("/?login=success")

    def _redirect(self, location: str):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()
