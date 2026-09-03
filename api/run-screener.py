import csv
import io
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, jsonify

from blob_store import upload_csv
from kite_web_auth import get_kite_session_from_token
from screener import run_screener
from token_store import load_access_token

app = Flask(__name__)


@app.route("/", defaults={"path": ""}, methods=["GET", "POST"])
@app.route("/<path:path>", methods=["GET", "POST"])
def index(path):
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
