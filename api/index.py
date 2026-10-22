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
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flask import Flask, Response, jsonify, redirect, request

import candidate_alert
import db_store
import nse_holidays_sync
import trade_csv_import
from blob_store import upload_csv
from breakout_config import config_from_dict
from breakout_screener import run_breakout_screener
from breakout_stats import summarize as summarize_breakout_events
from config import CONFIG
from cost_basis import replay as replay_cost_basis
from hv_config import CHARGES, config_from_dict as hv_config_from_dict
from hv_journal import break_even_price, build_journal, position_summary, target_price
from hv_screener import build_candidates as hv_build_candidates, get_quotes as hv_get_quotes, today_ist
from kite_web_auth import build_login_url, exchange_request_token, get_kite_session_from_token
from screener import run_screener, run_screener_symbol
from token_store import load_access_token, save_access_token

app = Flask(__name__)

_INDEX_HTML_PATH = Path(__file__).resolve().parent.parent / "index.html"
_COST_BASIS_HTML_PATH = Path(__file__).resolve().parent.parent / "cost_basis.html"
_ATRX_STOCK_HTML_PATH = Path(__file__).resolve().parent.parent / "atrx_stock.html"
_PERFORMANCE_LOG_HTML_PATH = Path(__file__).resolve().parent.parent / "performance_log.html"
_PERFORMANCE_LOG_WIDGET_JS_PATH = Path(__file__).resolve().parent.parent / "performance_log_widget.js"
_BREAKOUT_HTML_PATH = Path(__file__).resolve().parent.parent / "breakout.html"
_FIND_ATRX_OPPS_HTML_PATH = Path(__file__).resolve().parent.parent / "find_atrx_opps.html"
_HIGH_VOLATILITY_HTML_PATH = Path(__file__).resolve().parent.parent / "high_volatility.html"

CRON_SECRET = os.getenv("CRON_SECRET", "")


def _serve_file(path: Path, missing_message: str, mimetype: str = "text/html"):
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return Response(missing_message, status=500)
    return Response(content, mimetype=mimetype)


def home():
    return _serve_file(_INDEX_HTML_PATH, "index.html not found")


def cost_basis_page():
    return _serve_file(_COST_BASIS_HTML_PATH, "cost_basis.html not found")


def atrx_stock_page():
    return _serve_file(_ATRX_STOCK_HTML_PATH, "atrx_stock.html not found")


def performance_log_page():
    return _serve_file(_PERFORMANCE_LOG_HTML_PATH, "performance_log.html not found")


def performance_log_widget_js():
    return _serve_file(
        _PERFORMANCE_LOG_WIDGET_JS_PATH, "performance_log_widget.js not found",
        mimetype="application/javascript",
    )


def breakout_page():
    return _serve_file(_BREAKOUT_HTML_PATH, "breakout.html not found")


def find_atrx_opps_page():
    return _serve_file(_FIND_ATRX_OPPS_HTML_PATH, "find_atrx_opps.html not found")


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
        result = run_screener(kite, overrides=overrides, universe_tier=universe_tier,
                              save_atr_snapshot=True)
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


def breakout_config_endpoint():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        try:
            cfg = config_from_dict(body)
        except Exception as e:
            return jsonify({"error": f"Invalid config: {e}"}), 400
        try:
            db_store.save_breakout_config(cfg.to_tunable_dict())
        except Exception as e:
            return jsonify({"error": f"Could not save config: {e}"}), 500
        return jsonify(cfg.to_tunable_dict())

    try:
        persisted = db_store.load_breakout_config()
    except Exception:
        persisted = None
    from breakout_config import CONFIG as _DEFAULT_BREAKOUT_CONFIG
    return jsonify(persisted or _DEFAULT_BREAKOUT_CONFIG.to_tunable_dict())


def run_breakout_endpoint():
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
        result = run_breakout_screener(kite, overrides=overrides, universe_tier=universe_tier)
    except (FileNotFoundError, ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        import traceback
        return jsonify({
            "error": f"Unexpected {type(e).__name__}: {e}",
            "traceback": traceback.format_exc(),
        }), 500

    return jsonify(result)


def breakout_stats_endpoint():
    signal_config_hash = request.args.get("hash")
    if not signal_config_hash:
        return jsonify({"error": "hash is required (a run's signal_config_hash)"}), 400
    universe_tier = request.args.get("tier") or None

    try:
        events = db_store.load_signal_events(signal_config_hash, universe_tier)
    except Exception as e:
        return jsonify({"error": f"Could not load signal events: {e}"}), 500

    summary = summarize_breakout_events(events)
    summary["signal_config_hash"] = signal_config_hash
    summary["universe_tier"] = universe_tier
    return jsonify(summary)


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
        trades = db_store.load_symbol_trades(symbol)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    try:
        result = replay_cost_basis(symbol, trades)
    except Exception as e:
        # Replay failed (e.g. the oversell check) -- still return every raw
        # trade currently stored for this symbol so the failure is
        # diagnosable (compare against Kite's own records) instead of just
        # an error with nothing to look at.
        return jsonify({"error": str(e), "raw_trades": trades}), 500
    return jsonify(result)


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


def trades_endpoint():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        strategy = (body.get("strategy") or "").strip()
        symbol = (body.get("symbol") or "").strip().upper()
        qty = body.get("qty")
        entry_date = body.get("entry_date")
        entry_price = body.get("entry_price")
        if not strategy or not symbol or not qty or not entry_date or not entry_price:
            return jsonify({"error": "strategy, symbol, qty, entry_date, entry_price are all required"}), 400
        try:
            trade_id = db_store.create_trade(
                strategy, symbol, qty, entry_date, entry_price,
                notes=body.get("notes"), signal_meta=body.get("signal_meta"),
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"id": trade_id})

    strategy = request.args.get("strategy") or None
    try:
        trades = db_store.list_trades(strategy)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(trades)


def trades_close_endpoint():
    body = request.get_json(silent=True) or {}
    trade_id = body.get("trade_id")
    exit_date = body.get("exit_date")
    exit_price = body.get("exit_price")
    if not trade_id or not exit_date or not exit_price:
        return jsonify({"error": "trade_id, exit_date, exit_price are all required"}), 400
    try:
        db_store.close_trade(trade_id, exit_date, exit_price)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def trades_delete_endpoint():
    body = request.get_json(silent=True) or {}
    trade_id = body.get("trade_id")
    if not trade_id:
        return jsonify({"error": "trade_id is required"}), 400
    try:
        db_store.delete_trade(trade_id)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def trade_stats_endpoint():
    strategy = request.args.get("strategy") or None
    try:
        stats = db_store.get_trade_stats(strategy)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify(stats)


def trade_quotes_endpoint():
    """Live LTP for open positions -- Performance Log shows current price
    and marks unrealized P&L/return against it, but never stores it (same
    always-derived principle as pct_return/pnl in db_store.list_trades)."""
    symbols = sorted({s.strip().upper() for s in (request.args.get("symbols") or "").split(",") if s.strip()})
    if not symbols:
        return jsonify({})

    try:
        token = load_access_token()
    except RuntimeError as e:
        return jsonify({"error": f"Token store not configured: {e}"}), 500
    if not token:
        return jsonify({"error": "Not logged in. Log in with Zerodha first."}), 401

    keys = [f"{CONFIG.exchange}:{s}" for s in symbols]
    try:
        kite = get_kite_session_from_token(token)
        quotes = kite.ltp(keys)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

    return jsonify({
        sym: quotes[key]["last_price"]
        for sym, key in zip(symbols, keys) if key in quotes
    })


# ---------------------------------------------------------------------------
# High Volatility page (manual first-hour trading helper -- places no orders)
# ---------------------------------------------------------------------------

def high_volatility_page():
    return _serve_file(_HIGH_VOLATILITY_HTML_PATH, "high_volatility.html not found")


def _hv_config():
    try:
        persisted = db_store.load_hv_settings()
    except Exception:
        persisted = None
    return hv_config_from_dict(persisted)


def _kite_or_error():
    """(kite, None) when logged in, else (None, (json_response, status))."""
    try:
        token = load_access_token()
    except RuntimeError as e:
        return None, (jsonify({"error": f"Token store not configured: {e}"}), 500)
    if not token:
        return None, (jsonify({"error": "Not logged in. Log in with Zerodha first."}), 401)
    return get_kite_session_from_token(token), None


def _parse_symbols(raw: str) -> list[str]:
    seen, out = set(), []
    for s in (raw or "").split(","):
        s = s.strip().upper()
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    return out


def hv_settings_endpoint():
    cfg = _hv_config()
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        merged = cfg.to_dict()
        merged.update({k: v for k, v in body.items() if k in merged})
        try:
            cfg = hv_config_from_dict(merged)
            db_store.save_hv_settings(cfg.to_dict())
        except (TypeError, ValueError) as e:
            return jsonify({"error": f"Invalid setting: {e}"}), 400
        except Exception as e:
            return jsonify({"error": str(e)}), 500
    return jsonify({"settings": cfg.to_dict(), "charges": CHARGES})


def hv_candidates_endpoint():
    kite, err = _kite_or_error()
    if err:
        return err
    cfg = _hv_config()
    extras = _parse_symbols(request.args.get("extras"))
    force = request.args.get("refresh") == "1"
    try:
        if not db_store.load_atr_snapshot() and not extras:
            return jsonify({"error": "No ATR snapshot saved yet. Run Find ATRx Opps (all three "
                                     "universes) first, then come back here."}), 400
        result = hv_build_candidates(kite, cfg, extras, force=force)
    except (RuntimeError, ValueError) as e:
        return jsonify({"error": str(e)}), 500
    except Exception as e:
        import traceback
        return jsonify({"error": f"Unexpected {type(e).__name__}: {e}",
                        "traceback": traceback.format_exc()}), 500
    return jsonify(result)


def hv_live_endpoint():
    symbols = _parse_symbols(request.args.get("symbols"))
    if not symbols:
        return jsonify({"quotes": {}})
    kite, err = _kite_or_error()
    if err:
        return err
    try:
        quotes = hv_get_quotes(kite, symbols)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"quotes": quotes, "fetched_at": datetime.now().astimezone().isoformat()})


def hv_day_endpoint():
    if request.method == "POST":
        body = request.get_json(silent=True) or {}
        trade_date = body.get("trade_date") or today_ist().isoformat()
        focus = []
        for f in body.get("focus") or []:
            symbol = (f.get("symbol") or "").strip().upper()
            if not symbol:
                continue
            bias = f.get("bias") if f.get("bias") in ("long", "short", "either") else "long"
            focus.append({"symbol": symbol, "bias": bias, "tags": f.get("tags") or [],
                          "note": f.get("note"), "pre_meta": f.get("pre_meta")})
        try:
            db_store.save_hv_day(trade_date, body.get("gift_nifty"), body.get("note"), focus)
        except Exception as e:
            return jsonify({"error": str(e)}), 500
        return jsonify({"ok": True})

    date_str = request.args.get("date")
    try:
        trade_date = datetime.strptime(date_str, "%Y-%m-%d").date() if date_str else today_ist()
        return jsonify(db_store.load_hv_day(trade_date))
    except ValueError:
        return jsonify({"error": "date must be YYYY-MM-DD"}), 400
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _fill_args(body):
    """Validated (qty, price, filled_at) from a request body, or an error response."""
    try:
        qty, price = int(body.get("qty")), float(body.get("price"))
    except (TypeError, ValueError):
        return None, (jsonify({"error": "qty and price are required numbers"}), 400)
    if qty <= 0 or price <= 0:
        return None, (jsonify({"error": "qty and price must be positive"}), 400)
    return (qty, price, body.get("filled_at") or None), None


def hv_position_open_endpoint():
    body = request.get_json(silent=True) or {}
    symbol = (body.get("symbol") or "").strip().upper()
    side = body.get("side")
    if not symbol or side not in ("long", "short"):
        return jsonify({"error": "symbol and side (long|short) are required"}), 400
    args, err = _fill_args(body)
    if err:
        return err
    qty, price, filled_at = args
    try:
        trade_date = today_ist()
        for p in db_store.list_hv_positions(trade_date, trade_date):
            if p["symbol"] == symbol and position_summary(p["side"], p["fills"])["status"] == "open":
                return jsonify({"error": f"{symbol} already has an open position today -- use Add "
                                         "(scale in) or Exit on it."}), 409
        position_id = db_store.create_hv_position(
            trade_date, symbol, side, qty, price, filled_at,
            setup_tag=body.get("setup_tag"), note=body.get("note"), signal_meta=body.get("signal_meta"))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"id": position_id})


def hv_fill_endpoint():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    if kind not in ("entry", "exit") or not body.get("position_id"):
        return jsonify({"error": "position_id and kind (entry|exit) are required"}), 400
    args, err = _fill_args(body)
    if err:
        return err
    qty, price, filled_at = args
    try:
        position = db_store.get_hv_position(int(body["position_id"]))
        if position is None:
            return jsonify({"error": "position not found"}), 404
        summary = position_summary(position["side"], position["fills"])
        if kind == "exit" and qty > summary["open_qty"]:
            return jsonify({"error": f"only {summary['open_qty']} shares are open on this position"}), 400
        if kind == "entry" and summary["status"] == "closed":
            return jsonify({"error": "this position is closed -- start a new one"}), 400
        fill_id = db_store.add_hv_fill(position["id"], kind, qty, price, filled_at)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"id": fill_id})


def hv_fill_delete_endpoint():
    fill_id = (request.get_json(silent=True) or {}).get("fill_id")
    if not fill_id:
        return jsonify({"error": "fill_id is required"}), 400
    try:
        db_store.delete_hv_fill(int(fill_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def hv_position_update_endpoint():
    body = request.get_json(silent=True) or {}
    if not body.get("position_id"):
        return jsonify({"error": "position_id is required"}), 400
    rating = body.get("execution_rating") if body.get("execution_rating") in ("clean", "rushed") else None
    try:
        db_store.update_hv_position(int(body["position_id"]), body.get("setup_tag") or None,
                                    body.get("note") or None, rating)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def hv_position_delete_endpoint():
    position_id = (request.get_json(silent=True) or {}).get("position_id")
    if not position_id:
        return jsonify({"error": "position_id is required"}), 400
    try:
        db_store.delete_hv_position(int(position_id))
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return jsonify({"ok": True})


def hv_journal_endpoint():
    """Positions + per-day roll-ups + totals. `days` = how far back
    (calendar days, default 30; 0 = everything). All P&L is derived here
    from the raw fills -- nothing is stored."""
    try:
        days_back = int(request.args.get("days", 30))
    except ValueError:
        return jsonify({"error": "days must be an integer"}), 400
    cfg = _hv_config()
    today = today_ist()
    from_date = today - timedelta(days=days_back) if days_back > 0 else datetime(2000, 1, 1).date()
    try:
        journal = build_journal(db_store.list_hv_positions(from_date), cfg)
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    for p in journal["positions"]:
        s = p["summary"]
        if s["open_qty"] and s["avg_entry"]:
            s["break_even"] = break_even_price(p["side"], s["avg_entry"], s["open_qty"])
            s["targets"] = {str(t): target_price(p["side"], s["avg_entry"], s["open_qty"], t)
                            for t in cfg.target_pcts}
    today_summary = next((d for d in journal["days"] if d["date"] == today.isoformat()), None)
    journal["today"] = today.isoformat()
    journal["today_summary"] = today_summary
    return jsonify(journal)


_ROUTES = {
    "": home,
    "cost-basis": cost_basis_page,
    "atrx-stock": atrx_stock_page,
    "performance-log": performance_log_page,
    "performance-log-widget-js": performance_log_widget_js,
    "breakout": breakout_page,
    "find-atrx-opps": find_atrx_opps_page,
    "high-volatility": high_volatility_page,
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
    "trades": trades_endpoint,
    "trades-close": trades_close_endpoint,
    "trades-delete": trades_delete_endpoint,
    "trade-stats": trade_stats_endpoint,
    "trade-quotes": trade_quotes_endpoint,
    "breakout-config": breakout_config_endpoint,
    "run-breakout": run_breakout_endpoint,
    "breakout-stats": breakout_stats_endpoint,
    "hv-settings": hv_settings_endpoint,
    "hv-candidates": hv_candidates_endpoint,
    "hv-live": hv_live_endpoint,
    "hv-day": hv_day_endpoint,
    "hv-position-open": hv_position_open_endpoint,
    "hv-fill": hv_fill_endpoint,
    "hv-fill-delete": hv_fill_delete_endpoint,
    "hv-position-update": hv_position_update_endpoint,
    "hv-position-delete": hv_position_delete_endpoint,
    "hv-journal": hv_journal_endpoint,
}


@app.route("/api/index", methods=["GET", "POST"])
def dispatch():
    route = request.args.get("r", "")
    handler = _ROUTES.get(route)
    if handler is None:
        return jsonify({"error": "not found", "route": route}), 404
    return handler()
