import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from http.server import BaseHTTPRequestHandler

from kite_web_auth import build_login_url


class handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            url = build_login_url()
        except RuntimeError as e:
            self.send_response(500)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(str(e).encode())
            return

        self.send_response(302)
        self.send_header("Location", url)
        self.end_headers()
