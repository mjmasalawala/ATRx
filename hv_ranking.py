"""
Pre-open ranking for the High Volatility page. Answers "which tickers are
most likely to give me a dip-then-bounce (or pop-then-fade) of 1-3%,
twice, within the session" using each ticker's own recent 5-minute
history, not just daily ATR%.

Pure functions only (pandas in, dicts out) -- no Kite, no DB -- so the
logic is unit-testable on synthetic candles. api/index.py's endpoint owns
the Kite/DB plumbing.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd

from hv_config import HVConfig
from indicators import average_true_range_series

MARKET_OPEN_MINUTES = 9 * 60 + 15
MIN_CANDLES_PER_FULL_DAY = 60   # NSE's 9:15-15:30 session is 75 five-minute candles


def count_swings(prices: list[float], pct: float) -> tuple[int, int]:
    """Zigzag leg counter: (up_legs, down_legs), counting each move of at
    least `pct` percent between successive turning points. A leg is counted
    the moment it crosses the threshold, so a trailing unfinished move that
    already passed it is included."""
    if len(prices) < 2:
        return 0, 0
    thr = pct / 100.0
    up = down = 0
    direction = 0
    hi = lo = prices[0]
    for p in prices[1:]:
        if direction == 0:
            hi, lo = max(hi, p), min(lo, p)
            if p >= lo * (1 + thr):
                direction, up, hi = 1, up + 1, p
            elif p <= hi * (1 - thr):
                direction, down, lo = -1, down + 1, p
        elif direction == 1:
            if p > hi:
                hi = p
            elif p <= hi * (1 - thr):
                direction, down, lo = -1, down + 1, p
        else:
            if p < lo:
                lo = p
            elif p >= lo * (1 + thr):
                direction, up, hi = 1, up + 1, p
    return up, down


def _max_drawup_pct(lows: np.ndarray, highs: np.ndarray) -> float:
    """Biggest low-then-later-high rise, % of that low (the bounce a
    dip-buyer could have caught)."""
    running_low = np.minimum.accumulate(lows)
    return float(((highs - running_low) / running_low).max() * 100)


def _max_drawdown_pct(lows: np.ndarray, highs: np.ndarray) -> float:
    """Biggest high-then-later-low fall, % of that high (the fade a
    short-seller could have caught)."""
    running_high = np.maximum.accumulate(highs)
    return float(((running_high - lows) / running_high).max() * 100)


def _day_stats(day: pd.DataFrame, cfg: HVConfig) -> dict:
    day = day.sort_values("timestamp")
    opens = day["open"].to_numpy(dtype=float)
    highs = day["high"].to_numpy(dtype=float)
    lows = day["low"].to_numpy(dtype=float)
    closes = day["close"].to_numpy(dtype=float)
    minutes = day["timestamp"].dt.hour * 60 + day["timestamp"].dt.minute - MARKET_OPEN_MINUTES
    fh = (minutes < cfg.first_hour_minutes).to_numpy()
    day_open = opens[0]

    fh_highs, fh_lows = highs[fh], lows[fh]
    fh_range = (fh_highs.max() - fh_lows.min()) / day_open * 100 if fh.any() else 0.0
    up, down = count_swings(list(closes), cfg.swing_pct)
    path = float(np.abs(np.diff(closes)).sum())
    net = abs(closes[-1] - closes[0])
    efficiency = net / path if path else 1.0

    return {
        "fh_range_pct": fh_range,
        "day_range_pct": (highs.max() - lows.min()) / day_open * 100,
        "up_swings": up,
        "down_swings": down,
        "whipsaw": 1.0 - min(efficiency, 1.0),
        "max_bounce_pct": _max_drawup_pct(lows, highs),
        "max_fade_pct": _max_drawdown_pct(lows, highs),
        "volume": float(day["volume"].sum()),
        "close": float(closes[-1]),
        "high": float(highs.max()), "low": float(lows.min()), "open": float(day_open),
    }


def intraday_stats(candles: pd.DataFrame, cfg: HVConfig, today: date) -> dict | None:
    """Per-ticker statistics over recent *completed* sessions (today's
    partial session is excluded). candles: 5-minute frame with timestamp,
    open, high, low, close, volume. Returns None when there's too little
    history to be meaningful."""
    if candles is None or candles.empty:
        return None
    df = candles.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df["_date"] = df["timestamp"].dt.date
    df = df[df["_date"] != today]

    per_day = []
    for d, day in df.groupby("_date"):
        if len(day) >= MIN_CANDLES_PER_FULL_DAY:
            stats = _day_stats(day, cfg)
            stats["date"] = d
            per_day.append(stats)
    if len(per_day) < 5:
        return None
    per_day.sort(key=lambda s: s["date"])

    n = len(per_day)
    mean = lambda key: float(np.mean([s[key] for s in per_day]))  # noqa: E731
    last = per_day[-1]
    prior_volumes = [s["volume"] for s in per_day[:-1]]
    avg_prior_volume = float(np.mean(prior_volumes)) if prior_volumes else 0.0

    daily = pd.DataFrame([{"high": s["high"], "low": s["low"], "close": s["close"]} for s in per_day])
    atr_series = average_true_range_series(daily, 7)
    atr_pct = float(atr_series.iloc[-1] / last["close"] * 100) if not np.isnan(atr_series.iloc[-1]) else None

    return {
        "days": n,
        "avg_fh_range_pct": round(mean("fh_range_pct"), 2),
        "avg_day_range_pct": round(mean("day_range_pct"), 2),
        "avg_up_swings": round(mean("up_swings"), 2),
        "avg_down_swings": round(mean("down_swings"), 2),
        "two_up_days_pct": round(sum(s["up_swings"] >= 2 for s in per_day) / n * 100, 1),
        "two_down_days_pct": round(sum(s["down_swings"] >= 2 for s in per_day) / n * 100, 1),
        "avg_whipsaw": round(mean("whipsaw"), 3),
        "avg_max_bounce_pct": round(mean("max_bounce_pct"), 2),
        "avg_max_fade_pct": round(mean("max_fade_pct"), 2),
        "avg_turnover_cr": round(float(np.mean([s["volume"] * s["close"] for s in per_day])) / 1e7, 2),
        "last_volume_ratio": round(last["volume"] / avg_prior_volume, 2) if avg_prior_volume else None,
        "last_close": round(last["close"], 2),
        "atr_pct_5m": round(atr_pct, 2) if atr_pct is not None else None,
        "swing_pct": cfg.swing_pct,
        "first_hour_minutes": cfg.first_hour_minutes,
        "last_session": last["date"].isoformat(),
    }


def _norm(values: list[float]) -> list[float]:
    arr = np.array(values, dtype=float)
    lo, hi = arr.min(), arr.max()
    if hi - lo < 1e-12:
        return [0.5] * len(values)
    return list((arr - lo) / (hi - lo))


def rank_candidates(rows: list[dict], cfg: HVConfig) -> tuple[list[dict], list[dict]]:
    """rows: [{symbol, atr_pct, tier, manual, stats (intraday_stats result
    or None), quote ({last_price, prev_close, lower_circuit, upper_circuit}
    or None)}]. Returns (ranked, excluded). Each ranked row carries a 0-100
    `score` (pre-open clues are added client-side, since the user ticks them
    on the page) and a human-readable `why` list; each excluded row says
    why it was dropped."""
    eligible, excluded = [], []
    for r in rows:
        stats, q = r.get("stats"), r.get("quote") or {}
        reason = None
        # Tickers typed in by hand skip the volatility filter (the user only
        # adds one when they expect it to move today) but not the safety
        # checks: a failing turnover or price band is kept as a warning
        # badge instead of dropping the row.
        r["safety_warnings"] = []
        if stats is None:
            reason = r.get("missing_reason") or "not enough 5-minute history"
        else:
            atr = r.get("atr_pct") if r.get("atr_pct") is not None else stats.get("atr_pct_5m")
            r["atr_pct"] = atr
            thin = stats["avg_turnover_cr"] < cfg.min_avg_turnover_cr
            if r.get("manual"):
                if thin:
                    r["safety_warnings"].append(f"turnover {stats['avg_turnover_cr']} cr < {cfg.min_avg_turnover_cr:g}")
            elif atr is not None and atr < cfg.min_atr_pct:
                reason = f"ATR% {atr:.2f} below {cfg.min_atr_pct}"
            elif thin:
                reason = f"avg turnover {stats['avg_turnover_cr']} cr below {cfg.min_avg_turnover_cr}"
        prev_close, upper = q.get("prev_close"), q.get("upper_circuit")
        if reason is None and prev_close and upper:
            band = (upper - prev_close) / prev_close * 100
            r["circuit_band_pct"] = round(band, 1)
            if band < cfg.min_circuit_band_pct - 0.01:
                if r.get("manual"):
                    r["safety_warnings"].append(f"price band only {band:.0f}%")
                else:
                    reason = f"price band only {band:.0f}%"
        if reason:
            excluded.append({"symbol": r["symbol"], "reason": reason})
        else:
            eligible.append(r)
    if not eligible:
        return [], excluded

    def gap_pct(r):
        q = r.get("quote") or {}
        if q.get("last_price") and q.get("prev_close"):
            return (q["last_price"] - q["prev_close"]) / q["prev_close"] * 100
        return None

    for r in eligible:
        r["gap_pct"] = None if gap_pct(r) is None else round(gap_pct(r), 2)

    comps = {
        "fh": _norm([r["stats"]["avg_fh_range_pct"] for r in eligible]),
        "swing": _norm([max(r["stats"]["two_up_days_pct"], r["stats"]["two_down_days_pct"]) for r in eligible]),
        "atr": _norm([r["atr_pct"] or 0 for r in eligible]),
        "whip": _norm([r["stats"]["avg_whipsaw"] for r in eligible]),
        "gap": _norm([min(abs(r["gap_pct"] or 0), 3.0) for r in eligible]),
        "vol": _norm([min(r["stats"]["last_volume_ratio"] or 1.0, 3.0) for r in eligible]),
    }
    weights = {"fh": cfg.w_first_hour_range, "swing": cfg.w_two_swing_days, "atr": cfg.w_atr,
               "whip": cfg.w_whipsaw, "gap": cfg.w_gap, "vol": cfg.w_volume_surge}
    total_w = sum(weights.values()) or 1.0

    ranked = []
    for i, r in enumerate(eligible):
        s = r["stats"]
        score = sum(weights[k] * comps[k][i] for k in weights) / total_w * 100
        why = (["added by you (volatility filter skipped)"] if r.get("manual") else []) + [
            f"1st-hour range avg {s['avg_fh_range_pct']}%",
            f"2+ up-swings ≥{cfg.swing_pct}% on {s['two_up_days_pct']:.0f}% of days, "
            f"2+ down-swings on {s['two_down_days_pct']:.0f}%",
        ]
        if r["gap_pct"] is not None and abs(r["gap_pct"]) >= 0.5:
            why.append(f"pre-open gap {r['gap_pct']:+.2f}%")
        if s["last_volume_ratio"] and s["last_volume_ratio"] >= 1.5:
            why.append(f"last session volume {s['last_volume_ratio']}× average")
        ranked.append({
            "symbol": r["symbol"], "tier": r.get("tier"), "manual": bool(r.get("manual")),
            "atr_pct": r["atr_pct"], "gap_pct": r["gap_pct"],
            "last_price": (r.get("quote") or {}).get("last_price") or s["last_close"],
            "prev_close": (r.get("quote") or {}).get("prev_close"),
            "circuit_band_pct": r.get("circuit_band_pct"),
            "safety_warnings": r.get("safety_warnings") or [],
            "stats": s, "score": round(score, 1), "why": why,
        })
    ranked.sort(key=lambda x: -x["score"])
    return ranked, excluded
