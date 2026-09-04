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

import candidate_alert
import db_store
import nse_holidays_sync
import trade_csv_import
from blob_store import upload_csv
from config import CONFIG
from cost_basis import replay as replay_cost_basis
from kite_web_auth import build_login_url, exchange_request_token, get_kite_session_from_token
from screener import run_screener, run_screener_symbol
from token_store import load_access_token, save_access_token

app = Flask(__name__)

_INDEX_HTML_PATH = Path(__file__).resolve().parent.parent / "index.html"
_COST_BASIS_HTML_PATH = Path(__file__).resolve().parent.parent / "cost_basis.html"
_ATRX_STOCK_HTML_PATH = Path(__file__).resolve().parent.parent / "atrx_stock.html"

CRON_SECRET = os.getenv("CRON_SECRET", "")


def _serve_html(path: Path, missing_message: str):
    try:
        html = path.read_text(encoding="utf-8")
    except OSError:
        return Response(missing_message, status=500)
    return Response(html, mimetype="text/html")


def home():
    return _serve_html(_INDEX_HTML_PATH, "index.html not found")


def cost_basis_page():
    return _serve_html(_COST_BASIS_HTML_PATH, "cost_basis.html not found")


def atrx_stock_page():
    return _serve_html(_ATRX_STOCK_HTML_PATH, "atrx_stock.html not found")


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


def run_screener_symbol_endpoint():
    try:
        token = load_access_token()
    except RuntimeError as e:
        return jsonify({"error": f"Token store not configured: {e}"}), 500

    if not token:
        return jsonify({"error": "Not logged in. Log in with Zerodha first."}), 401

    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip()
    if not symbol:
        return jsonify({"error": "symbol is required"}), 400
    overrides = body.get("overrides") or None

    try:
        kite = get_kite_session_from_token(token)
        result = run_screener_symbol(kite, symbol, overrides=overrides)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Unexpected {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500

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


def cost_basis_summary_endpoint():
    try:
        rows = db_store.list_cost_basis_summary()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(rows)


def cost_basis_ledger_endpoint():
    symbol = (request.args.get("symbol") or "").strip().upper()
    if not symbol:
        return jsonify({"error": "symbol query param is required"}), 400
    try:
        baseline = db_store.load_baseline_row(symbol)
        trades = db_store.load_symbol_trades(symbol)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(replay_cost_basis(symbol, baseline, trades))


def cost_basis_upload_trades_endpoint():
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "No file uploaded (expected a 'file' field)."}), 400

    try:
        content = upload.stream.read().decode("utf-8-sig")
    except UnicodeDecodeError as e:
        return jsonify({"error": f"Could not read file as UTF-8 CSV: {e}"}), 400

    try:
        result = trade_csv_import.import_trades_csv(content)
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Unexpected {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500

    return jsonify(result)


def cost_basis_sync_status_endpoint():
    try:
        last_synced_at = db_store.get_last_trades_sync()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"last_synced_at": last_synced_at})


def scan_candidates_endpoint():
    provided = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    tier = (request.args.get("tier") or "").strip()
    if not tier:
        return jsonify({"error": "tier query param is required"}), 400

    try:
        result = candidate_alert.run_candidate_scan(tier)
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Unexpected {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500
    return jsonify(result)


def sync_nse_holidays_endpoint():
    provided = (request.headers.get("Authorization") or "").removeprefix("Bearer ").strip()
    if not CRON_SECRET or provided != CRON_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    year = request.args.get("year", type=int)
    try:
        result = nse_holidays_sync.sync_year(year)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(result)


_ROUTES = {
    "": home,
    "cost-basis": cost_basis_page,
    "atrx-stock": atrx_stock_page,
    "login": login,
    "callback": callback,
    "status": status_endpoint,
    "config": config_endpoint,
    "universe-tiers": universe_tiers_endpoint,
    "run-screener": run_screener_endpoint,
    "run-screener-symbol": run_screener_symbol_endpoint,
    "cost-basis-summary": cost_basis_summary_endpoint,
    "cost-basis-ledger": cost_basis_ledger_endpoint,
    "cost-basis-upload-trades": cost_basis_upload_trades_endpoint,
    "cost-basis-sync-status": cost_basis_sync_status_endpoint,
    "scan-candidates": scan_candidates_endpoint,
    "sync-nse-holidays": sync_nse_holidays_endpoint,
}


@app.route("/api/index", methods=["GET", "POST"])
def dispatch():
    route = request.args.get("r", "")
    handler = _ROUTES.get(route)
    if handler is None:
        return jsonify({"error": "not found", "route": route}), 404
    return handler()
