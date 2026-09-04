"""
The 15-min candidate-scan agent. Triggered externally (GitHub Actions cron,
since Vercel Hobby can't fire cron this often -- see
.github/workflows/scan-candidates.yml) once per universe tier per firing via
/api/scan-candidates?tier=<tier>.

Each call:
  1. Confirms it's actually inside the trading window on a trading day
     (belt-and-braces -- the external scheduler's cron expression already
     restricts this to roughly the right window, but doesn't know NSE
     holidays and can drift).
  2. Runs the same screener pipeline the web UI uses, with config.py's
     defaults (never the UI's saved/tunable overrides -- this agent's
     parameters are meant to be fixed, independent of whatever's being
     experimented with interactively).
  3. Sends a WhatsApp message for any candidate not already alerted today,
     ranked by volatility (ATR%) then by closeness of CMP to the level.
"""

import logging
from datetime import datetime, time as dt_time
from zoneinfo import ZoneInfo

import db_store
import whatsapp_notify
from kite_web_auth import get_kite_session_from_token
from screener import run_screener
from token_store import load_access_token

logger = logging.getLogger("atrx.candidate_alert")

IST = ZoneInfo("Asia/Kolkata")
WINDOW_START = dt_time(8, 50)
WINDOW_END = dt_time(15, 0)


def _is_trading_window(now_ist: datetime) -> tuple[bool, str]:
    if now_ist.weekday() >= 5:
        return False, "weekend"
    if not (WINDOW_START <= now_ist.time() <= WINDOW_END):
        return False, "outside scan window (8:50am-3:00pm IST)"
    try:
        if db_store.is_nse_holiday(now_ist.date()):
            return False, "NSE trading holiday"
    except Exception as e:
        # If the holiday table is unreachable, fail open on the day-check --
        # missing an alert on an actual holiday is far cheaper than silently
        # skipping every real trading day because of a transient DB error.
        logger.warning("Could not check NSE holiday table, assuming trading day: %s", e)
    return True, ""


def _format_message(tier: str, rows: list[dict]) -> str:
    lines = [f"ATRx candidates ({tier}) -- {len(rows)} new"]
    for r in rows:
        lines.append(
            f"{r['symbol']}: CMP {r['current_price']} | level {r['level']} | "
            f"ATRx {r['atrx']} | ATR% {r['atr_pct']}"
        )
    return "\n".join(lines)


def run_candidate_scan(tier: str) -> dict:
    now_ist = datetime.now(IST)

    ok, reason = _is_trading_window(now_ist)
    if not ok:
        return {"tier": tier, "sent": 0, "skipped": reason}

    try:
        token = load_access_token()
    except RuntimeError as e:
        return {"tier": tier, "sent": 0, "skipped": f"token store not configured: {e}"}

    if not token:
        notified = whatsapp_notify.send_login_needed_whatsapp()
        return {"tier": tier, "sent": 0, "skipped": "not logged in", "notified": notified}

    kite = get_kite_session_from_token(token)
    result = run_screener(kite, overrides=None, universe_tier=tier)
    rows = result["rows"]

    if not rows:
        return {"tier": tier, "sent": 0, "candidate_count": 0}

    alert_date = now_ist.date()
    candidate_keys = [(r["symbol"], r["level"]) for r in rows]
    unsent_keys = set(db_store.filter_unsent_candidates(alert_date, candidate_keys))

    new_rows = [r for r in rows if (r["symbol"], r["level"]) in unsent_keys]
    if not new_rows:
        return {"tier": tier, "sent": 0, "candidate_count": len(rows), "reason": "all already alerted today"}

    # Desc volatility (ATR%), then desc closeness of CMP to the level
    # (smaller |ATRx| = closer).
    new_rows.sort(key=lambda r: (-r["atr_pct"], abs(r["atrx"])))

    sent = whatsapp_notify.send_whatsapp(_format_message(tier, new_rows))
    if sent:
        db_store.record_alerts_sent(alert_date, [(r["symbol"], r["level"]) for r in new_rows])

    return {
        "tier": tier,
        "sent": len(new_rows) if sent else 0,
        "candidate_count": len(rows),
        "symbols": [r["symbol"] for r in new_rows],
        "whatsapp_sent": sent,
    }
