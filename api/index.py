"""
Single Flask app backing every route on the deployed site. Vercel's current
Python runtime builds one WSGI entrypoint per project (declared in
pyproject.toml), and its rewrite mechanism was found to flatten every
request's path to this function's own route ("/api/index") rather than
preserving the original path in the WSGI environ -- so Flask's normal
path-based @app.route dispatch can't tell requests apart. Instead, each
vercel.json rewrite rule appends the intended route as a `?r=` query
param, and this module dispatches on that explicitly.
"""

import csv
import io
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, jsonify, redirect, request

import db_store
from blob_store import upload_csv
from config import CONFIG
from kite_web_auth import build_login_url, exchange_request_token, get_kite_session_from_token
from screener import run_screener
from token_store import load_access_token, save_access_token

app = Flask(__name__)

_INDEX_HTML_PATH = Path(__file__).resolve().parent.parent / "index.html"
_STRATEGY2_HTML_PATH = Path(__file__).resolve().parent.parent / "strategy2.html"


def _serve_html(path: Path, missing_message: str):
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return Response(missing_message, status=500)
    return Response(html, mimetype="text/html")


def home():
    return _serve_html(_INDEX_HTML_PATH, "index.html not found")


def strategy2():
    return _serve_html(_STRATEGY2_HTML_PATH, "strategy2.html not found")


def login():
    try:
        url = build_login_url()
    except RuntimeError as e:
        return Response(str(e), status=500)
    return redirect(url)


def callback():
    status = request.args.get("status")
    request_token = request.args.get("request_token")

    if status != "success" or not request_token:
        return redirect("/?login=failed")

    try:
        access_token = exchange_request_token(request_token)
        save_access_token(access_token)
    except RuntimeError as e:
        # Temporary: surface the real failure reason in the redirect while
        # diagnosing a login issue. Revert to a bare "/?login=failed" once
        # the flow is confirmed working end to end.
        from urllib.parse import quote
        return redirect(f"/?login=failed&reason={quote(str(e))}")

    return redirect("/?login=success")


def status_endpoint():
    try:
        token = load_access_token()
    except RuntimeError:
        token = None
    return jsonify({"logged_in": bool(token)})


def config_endpoint():
    try:
        persisted = db_store.load_config()
    except Exception:
        persisted = None
    return jsonify(persisted or CONFIG.to_tunable_dict())


# Static fallback so the tier dropdown always has options even if the DB
# is unreachable -- symbol_count here is informational only (the real
# count comes from the DB row db_store.list_universe_tiers() reads).
_FALLBACK_TIERS = [
    {"tier": "large_cap", "note": "NSE Nifty 100 (large cap)", "symbol_count": None},
    {"tier": "mid_cap", "note": "NSE Nifty Midcap 150 (mid cap)", "symbol_count": None},
    {"tier": "small_cap", "note": "NSE Nifty Smallcap 100 (small cap)", "symbol_count": None},
]


def universe_tiers_endpoint():
    try:
        tiers = db_store.list_universe_tiers()
    except Exception:
        tiers = []
    return jsonify(tiers or _FALLBACK_TIERS)


def run_screener_endpoint():
    try:
        token = load_access_token()
    except RuntimeError as e:
        return jsonify({"error": f"Token store not configured: {e}"}), 500

    if not token:
        return jsonify({"error": "Not logged in. Log in with Zerodha first."}), 401

    body = request.get_json(silent=True) or {}
    overrides = body.get("overrides") or None
    universe_tier = body.get("universe_tier") or "large_cap"

    try:
        kite = get_kite_session_from_token(token)
        result = run_screener(kite, overrides=overrides, universe_tier=universe_tier)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        # Temporary: run_screener crashed with something other than the
        # expected exception types above (500 HTML page, no detail). Report
        # the real type/traceback while diagnosing, then narrow this back
        # down to the specific exception once the cause is fixed.
        import traceback
        return jsonify({
            "error": f"Unexpected {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500

    result["csv_url"], result["_csv_debug_error"] = _store_csv_snapshot(result["top_rows"])
    return jsonify(result)


def _store_csv_snapshot(rows: list[dict]):
    if not rows:
        return None, None
    try:
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
        pathname = f"atrx/atrx_{datetime.now():%Y%m%d_%H%M}.csv"
        return upload_csv(pathname, buf.getvalue().encode()), None
    except Exception as e:
        # Archival is best-effort -- any failure here (missing token, a bad
        # Blob API response, a network error) must not break the screener
        # results the UI is waiting on. _csv_debug_error is temporary, to
        # see the real reason instead of silently losing the archive.
        return None, f"{type(e).__name__}: {e}"


_ROUTES = {
    "": home,
    "strategy2": strategy2,
    "login": login,
    "callback": callback,
    "status": status_endpoint,
    "config": config_endpoint,
    "universe-tiers": universe_tiers_endpoint,
    "run-screener": run_screener_endpoint,
}


@app.route("/api/index", methods=["GET", "POST"])
def dispatch():
    route = request.args.get("r", "")
    handler = _ROUTES.get(route)
    if handler is None:
        return jsonify({"error": "not found", "route": route}), 404
    return handler()
