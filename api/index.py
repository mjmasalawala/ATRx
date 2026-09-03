"""
Single Flask app serving all /api/* routes. Vercel's current Python
runtime builds one WSGI entrypoint per project (declared in
pyproject.toml), not one function per file, so every route lives here and
Flask does the internal dispatch.
"""

import csv
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, jsonify, redirect, request

from blob_store import upload_csv
from kite_web_auth import build_login_url, exchange_request_token, get_kite_session_from_token
from screener import run_screener
from token_store import load_access_token, save_access_token

app = Flask(__name__)


@app.route("/api/login")
def login():
    try:
        url = build_login_url()
    except RuntimeError as e:
        return Response(str(e), status=500)
    return redirect(url)


@app.route("/api/callback")
def callback():
    status = request.args.get("status")
    request_token = request.args.get("request_token")

    if status != "success" or not request_token:
        return redirect("/?login=failed")

    try:
        access_token = exchange_request_token(request_token)
        save_access_token(access_token)
    except RuntimeError:
        return redirect("/?login=failed")

    return redirect("/?login=success")


@app.route("/api/status")
def status():
    try:
        token = load_access_token()
    except RuntimeError:
        token = None
    return jsonify({"logged_in": bool(token)})


@app.route("/api/run-screener", methods=["GET", "POST"])
def run_screener_endpoint():
    try:
        token = load_access_token()
    except RuntimeError as e:
        return jsonify({"error": f"Token store not configured: {e}"}), 500

    if not token:
        return jsonify({"error": "Not logged in. Log in with Zerodha first."}), 401

    try:
        kite = get_kite_session_from_token(token)
        result = run_screener(kite)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500

    result["csv_url"] = _store_csv_snapshot(result["top_rows"])
    return jsonify(result)


def _store_csv_snapshot(rows: list[dict]):
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
