"""
Setup detection for the High Volatility live tab (long side): turns today's
1-minute candles, the stock's pre-open context and the live quote into the
card's checklist and stage

    watching -> stretched -> armed -> entry        (or blocked / closed)

plus, for an open position, the management prompts (move stop, sell half,
trail, add, exit). Also scores a stored signal against what price did next
and replays past sessions through the same detector.

Pure functions only -- no Kite, no DB -- so the rules are unit-testable on
synthetic candles and the replay runs exactly the code the live tab runs.
Candles are dicts {ts (naive IST datetime, bar start), open, high, low,
close, volume}. The thresholds live in HVConfig; the reasoning behind each
one is in the "High Volatility Strategy Risk Review" doc.
"""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta

from hv_config import HVConfig

SESSION_OPEN = time(9, 15)
TICK = 0.05


# ---------------------------------------------------------------- helpers

def _hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def round_tick(price: float) -> float:
    return round(round(price / TICK) * TICK, 2)


def floor_tick(price: float) -> float:
    return round(math.floor(price / TICK + 1e-9) * TICK, 2)


def closed_bars(bars: list[dict], now: datetime, minutes: int = 1) -> list[dict]:
    """Drop the candle that is still forming (Kite returns it too)."""
    return [b for b in bars if b["ts"] + timedelta(minutes=minutes) <= now]


def resample(bars_1m: list[dict], minutes: int = 5) -> list[dict]:
    """1-minute bars -> `minutes` bars aligned to 9:15 (complete or not)."""
    out: list[dict] = []
    for b in bars_1m:
        offset = (b["ts"].hour * 60 + b["ts"].minute) - (9 * 60 + 15)
        start = b["ts"] - timedelta(minutes=offset % minutes, seconds=b["ts"].second)
        if out and out[-1]["ts"] == start:
            o = out[-1]
            o["high"], o["low"] = max(o["high"], b["high"]), min(o["low"], b["low"])
            o["close"] = b["close"]
            o["volume"] += b["volume"]
        else:
            out.append({"ts": start, "open": b["open"], "high": b["high"], "low": b["low"],
                        "close": b["close"], "volume": b["volume"]})
    return out


def rsi(closes: list[float], period: int) -> float | None:
    """Wilder RSI of the last close; None until there are period+1 closes."""
    if len(closes) < period + 1:
        return None
    gains, losses = [], []
    for a, b in zip(closes[:-1], closes[1:]):
        gains.append(max(b - a, 0.0))
        losses.append(max(a - b, 0.0))
    avg_g = sum(gains[:period]) / period
    avg_l = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        avg_g = (avg_g * (period - 1) + g) / period
        avg_l = (avg_l * (period - 1) + l) / period
    if avg_l == 0:
        return 100.0
    return 100 - 100 / (1 + avg_g / avg_l)


def vwap_bands(bars: list[dict]) -> list[tuple[float, float]]:
    """(vwap, sigma) after each bar: cumulative volume-weighted mean of the
    typical price and the volume-weighted spread around it. Bars with no
    volume (an index) fall back to equal weights, i.e. a TWAP."""
    out = []
    use_volume = any(b["volume"] for b in bars)
    sv = spv = spv2 = 0.0
    for b in bars:
        tp = (b["high"] + b["low"] + b["close"]) / 3
        v = b["volume"] if use_volume else 1.0
        sv += v
        spv += v * tp
        spv2 += v * tp * tp
        if sv <= 0:
            out.append((tp, 0.0))
            continue
        mean = spv / sv
        out.append((mean, math.sqrt(max(spv2 / sv - mean * mean, 0.0))))
    return out


def round_step(price: float) -> float:
    if price < 200:
        return 5.0
    if price < 1000:
        return 10.0
    if price < 5000:
        return 50.0
    return 100.0


# --------------------------------------------------------------- context

def build_context(history_5m: list[dict], today: date, sessions: int = 10) -> dict | None:
    """Pre-open context from completed sessions of 5-minute candles:
    previous day's high/low/close, daily ATR(14), each 5-minute slot's
    normal volume (median over the last `sessions` days), the average
    traded value of each 5-minute slot (liquidity cap) and the last
    closes of the previous session (so RSI(5) works from 9:20)."""
    by_day: dict[date, list[dict]] = {}
    for b in history_5m:
        d = b["ts"].date()
        if d < today:
            by_day.setdefault(d, []).append(b)
    days = sorted(d for d, bs in by_day.items() if len(bs) >= 30)
    if len(days) < 3:
        return None

    daily = []
    for d in days:
        bs = sorted(by_day[d], key=lambda b: b["ts"])
        daily.append({"high": max(b["high"] for b in bs), "low": min(b["low"] for b in bs),
                      "close": bs[-1]["close"]})
    trs = []
    for prev, cur in zip(daily[:-1], daily[1:]):
        trs.append(max(cur["high"] - cur["low"], abs(cur["high"] - prev["close"]),
                       abs(cur["low"] - prev["close"])))
    atr = sum(trs[-14:]) / len(trs[-14:]) if trs else daily[-1]["high"] - daily[-1]["low"]

    recent = days[-sessions:]
    slots: dict[str, list[float]] = {}
    fh_values = []
    value_slots: dict[str, list[float]] = {}
    for d in recent:
        for b in by_day[d]:
            key = b["ts"].strftime("%H:%M")
            slots.setdefault(key, []).append(float(b["volume"]))
            value_slots.setdefault(key, []).append(b["volume"] * b["close"])
            if b["ts"].time() < time(10, 15):
                fh_values.append(b["volume"] * b["close"])

    def median(xs):
        xs = sorted(xs)
        n = len(xs)
        return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2

    last = sorted(by_day[days[-1]], key=lambda b: b["ts"])
    return {
        "prev_high": daily[-1]["high"], "prev_low": daily[-1]["low"], "prev_close": daily[-1]["close"],
        "atr": round(atr, 4),
        "vol_baseline": {k: median(v) for k, v in slots.items()},
        "value_baseline": {k: round(median(v), 2) for k, v in value_slots.items()},
        "avg_5m_value": round(sum(fh_values) / len(fh_values), 2) if fh_values else 0.0,
        "prev_closes_5m": [b["close"] for b in last[-10:]],
        "sessions": len(days),
        "last_session": days[-1].isoformat(),
    }


# ---------------------------------------------------------------- day type

def nifty_state(bars_1m: list[dict], now: datetime) -> dict:
    """Range / Trend up / Trend down for the index from its 1-minute bars:
    side of its time-weighted average (the index has no volume) and whether
    it made a new session low/high in the last 3 bars."""
    bars = closed_bars(bars_1m, now)
    if len(bars) < 5:
        return {"state": "Range", "detail": "too early to tell"}
    vw, _ = vwap_bands(bars)[-1]
    price = bars[-1]["close"]
    session_low = min(b["low"] for b in bars)
    session_high = max(b["high"] for b in bars)
    recent_low = min(b["low"] for b in bars[-3:])
    recent_high = max(b["high"] for b in bars[-3:])
    if price < vw and recent_low <= session_low:
        return {"state": "Trend down", "detail": "below its average, making new lows", "price": price}
    if price > vw and recent_high >= session_high:
        return {"state": "Trend up", "detail": "above its average, making new highs", "price": price}
    return {"state": "Range", "detail": "no new extreme in the last 3 minutes", "price": price}


def day_type(bars: list[dict], five: list[dict], bands: list[tuple[float, float]], ctx: dict,
             cfg: HVConfig) -> dict:
    """The stock's own day type. Trend down = below a falling VWAP that it
    has barely crossed, or the opening range broken on 2x+ volume."""
    vw = bands[-1][0]
    price = bars[-1]["close"]
    back = bands[-16][0] if len(bands) >= 16 else bands[0][0]
    slope_pct = (vw - back) / vw * 100 if vw else 0.0
    after_or = [b for b, (v, _) in zip(bars, bands) if b["ts"].time() >= time(9, 20)]
    sides = [1 if b["close"] > v else -1
             for b, (v, _) in zip(bars, bands) if b["ts"].time() >= time(9, 20)]
    crosses = sum(1 for a, b in zip(sides[:-1], sides[1:]) if a != b)
    or_bars = [b for b in bars if b["ts"].time() < time(9, 20)]
    or_low = min(b["low"] for b in or_bars) if or_bars else None
    or_high = max(b["high"] for b in or_bars) if or_bars else None

    # A break of the opening range on heavy volume only marks a trend day
    # once it has held for 15 minutes -- the break bar itself is often the
    # capitulation a dip-buy wants.
    break_end = None
    for b in five[1:]:
        base = ctx["vol_baseline"].get(b["ts"].strftime("%H:%M")) or 0
        if or_low is not None and b["close"] < or_low and base and b["volume"] >= cfg.volume_spike_x * base:
            break_end = b["ts"] + timedelta(minutes=5)
            break
    held = False
    if break_end is not None:
        since = [b for b in bars if b["ts"] >= break_end]
        held = (bars[-1]["ts"] + timedelta(minutes=1) - break_end >= timedelta(minutes=15)
                and all(b["close"] < or_low for b in since))
    info = {"vwap_slope_pct": round(slope_pct, 3), "vwap_crosses": crosses,
            "or_low": or_low, "or_high": or_high}

    if held:
        return {**info, "state": "Trend down", "detail": f"opening range broken on {cfg.volume_spike_x:g}x+ volume and held"}
    if len(after_or) >= 20 and price < vw and slope_pct < -0.1 and crosses <= 1:
        return {**info, "state": "Trend down", "detail": "below a falling VWAP"}
    if len(after_or) >= 20 and price > vw and slope_pct > 0.1 and crosses <= 1:
        return {**info, "state": "Trend up", "detail": "above a rising VWAP"}
    return {**info, "state": "Range", "detail": f"{crosses} VWAP crossings"}


# --------------------------------------------------------------- evaluate

_STRENGTH = ("sweep and reclaim", "higher low", "close above prior high")


def _confirmations(bars: list[dict], flush_i: int) -> list[tuple[str, int]]:
    """Every confirmation after the flush low, as (kind, bar index it fired
    on): a sweep of the prior low that closes back above it (within 3
    bars), a close above the high of a higher low, or a close above the
    previous bar's high."""
    flush_low = bars[flush_i]["low"]
    events: list[tuple[str, int]] = []

    prior = bars[:max(flush_i - 2, 0)]
    if prior:
        prior_low = min(b["low"] for b in prior)
        if flush_low < prior_low:
            for j in range(flush_i, min(flush_i + 3, len(bars))):
                if bars[j]["close"] > prior_low:
                    events.append(("sweep and reclaim", j))
                    break

    for k in range(flush_i + 2, len(bars) - 1):
        b = bars[k]
        if b["low"] > flush_low and b["low"] < bars[k - 1]["low"] and b["low"] <= bars[k + 1]["low"]:
            j = next((j for j in range(k + 1, len(bars)) if bars[j]["close"] > b["high"]), None)
            if j is not None:
                events.append(("higher low", j))

    for j in range(flush_i + 1, len(bars)):
        if bars[j]["close"] > bars[j - 1]["high"]:
            events.append(("close above prior high", j))
    return events


def _pick_confirmation(events: list[tuple[str, int]], last_i: int, valid_bars: int) -> tuple[str | None, int | None]:
    """The strongest confirmation that fired within the last `valid_bars`
    bars; failing that, the most recent one (shown, but too old to act on)."""
    fresh = [e for e in events if last_i - e[1] < valid_bars]
    pool = fresh or sorted(events, key=lambda e: e[1])[-1:]
    if not pool:
        return None, None
    kind, j = min(pool, key=lambda e: (_STRENGTH.index(e[0]), -e[1]))
    return kind, j


def evaluate(bars_1m: list[dict], ctx: dict | None, quote: dict | None, nifty: dict | None,
             cfg: HVConfig, now: datetime, one_r: float | None = None,
             exposure_left: float | None = None, size_mult: float = 1.0) -> dict:
    """One stock's card at `now`. Returns {stage, reasons, day_type,
    metrics, checks, suggestion, episode}. `one_r`/`exposure_left` size the
    suggestion (defaults: cfg.one_r, unlimited)."""
    from hv_risk import size_trade  # local: hv_risk imports nothing from here, keeps the graph simple

    out = {"stage": "watching", "reasons": [], "day_type": None, "metrics": {}, "checks": {},
           "suggestion": None, "episode": None}
    if ctx is None:
        out["reasons"].append("no pre-open context -- press Start watch")
        return out
    bars = closed_bars(bars_1m, now)
    if not bars:
        out["reasons"].append("waiting for the first 1-minute bar")
        return out
    five = closed_bars(resample(bars), now, 5)
    bands = vwap_bands(bars)
    vw, sigma = bands[-1]
    q = quote or {}
    price = q.get("last_price") or bars[-1]["close"]
    atr = ctx.get("atr") or 0.0
    session_high = max(b["high"] for b in bars)
    m = out["metrics"]
    m.update({"price": price, "vwap": round(vw, 2), "sigma": round(sigma, 4), "atr": atr,
              "session_high": session_high, "session_low": min(b["low"] for b in bars)})

    dt = day_type(bars, five, bands, ctx, cfg)
    out["day_type"] = dt

    # -- stretch
    sig_dist = (price - vw) / sigma if sigma > 0 else 0.0
    atr_off = (session_high - price) / atr if atr else 0.0
    stretched = sig_dist <= -cfg.stretch_sigma or atr_off >= cfg.stretch_atr
    m["stretch_sigma"] = round(sig_dist, 2)
    m["off_high_atr"] = round(atr_off, 2)

    # -- flush low: lowest low of the last 30 minutes
    window = 30
    start_i = max(0, len(bars) - window)
    flush_i = min(range(start_i, len(bars)), key=lambda i: bars[i]["low"])
    flush = bars[flush_i]
    m["flush_low"] = flush["low"]
    m["flush_at"] = flush["ts"].strftime("%H:%M")
    out["episode"] = m["flush_at"]

    # -- exhaustion, on closed 5-minute bars
    signs = []
    closes = list(ctx.get("prev_closes_5m") or []) + [b["close"] for b in five]
    r = rsi(closes, cfg.rsi_period)
    m["rsi"] = round(r, 1) if r is not None else None
    if r is not None and r < cfg.rsi_max:
        signs.append("rsi")
    spike = None
    for i in range(max(0, len(five) - 3), len(five) - 1):
        b = five[i]
        base = ctx["vol_baseline"].get(b["ts"].strftime("%H:%M")) or 0
        if base and b["close"] < b["open"] and b["volume"] >= cfg.volume_spike_x * base \
                and five[i + 1]["volume"] < b["volume"]:
            spike = round(b["volume"] / base, 1)
    m["volume_x"] = spike
    if five:
        b = five[-1]
        base = ctx["vol_baseline"].get(b["ts"].strftime("%H:%M")) or 0
        m["last_volume_x"] = round(b["volume"] / base, 1) if base else None
    if spike:
        signs.append("volume")
    recent5 = five[-3:]
    wick_bar = min(recent5, key=lambda b: b["low"]) if recent5 else None
    wick = 0.0
    if wick_bar and wick_bar["high"] > wick_bar["low"]:
        wick = (min(wick_bar["open"], wick_bar["close"]) - wick_bar["low"]) / (wick_bar["high"] - wick_bar["low"])
    m["wick_frac"] = round(wick, 2)
    if wick >= cfg.wick_frac:
        signs.append("wick")
    levels = {"previous day low": ctx.get("prev_low"), "previous close": ctx.get("prev_close"),
              "open": q.get("open") or bars[0]["open"]}
    if dt["or_low"] is not None and flush["ts"].time() >= time(9, 20):
        levels["opening-range low"] = dt["or_low"]
    step = round_step(flush["low"])
    levels["round number"] = round(flush["low"] / step) * step
    tol = cfg.level_atr * atr if atr else flush["low"] * 0.001
    hit = next((name for name, lv in levels.items() if lv and abs(flush["low"] - lv) <= tol), None)
    m["level"] = hit
    if hit:
        signs.append("level")
    m["exhaustion"] = signs
    exhausted = len(signs) >= 2

    # -- confirmation, on closed 1-minute bars
    kind, ci = _pick_confirmation(_confirmations(bars, flush_i), len(bars) - 1, cfg.entry_valid_bars)
    m["confirmation"] = kind
    bars_since = (len(bars) - 1 - ci) if ci is not None else None
    m["confirm_bars_ago"] = bars_since

    # -- checks from the quote
    checks = out["checks"]
    depth = q.get("depth") or {}
    bid = (depth.get("buy") or [{}])[0].get("price") if depth else None
    ask = (depth.get("sell") or [{}])[0].get("price") if depth else None
    if bid and ask and ask >= bid:
        checks["spread_pct"] = round((ask - bid) / ((ask + bid) / 2) * 100, 3)
    lower, upper, prev = q.get("lower_circuit"), q.get("upper_circuit"), q.get("prev_close") or ctx.get("prev_close")
    if lower:
        checks["lower_band_pct"] = round((price - lower) / price * 100, 2)
    if upper:
        checks["upper_band_pct"] = round((upper - price) / price * 100, 2)
    if upper and prev:
        checks["band_width_pct"] = round((upper - prev) / prev * 100, 1)

    # -- blocks
    t = now.time()
    blocks = []
    if dt["state"] == "Trend down":
        blocks.append(f"Trend down: {dt['detail']}")
    if nifty and nifty.get("state") == "Trend down":
        blocks.append("Nifty trending down")
    gap = None
    if atr and ctx.get("prev_close"):
        gap = (bars[0]["open"] - ctx["prev_close"]) / atr
        m["gap_atr"] = round(gap, 2)
        reclaimed = any(b["close"] > v for b, (v, _) in zip(bars, bands))
        if gap <= -cfg.gap_atr and not reclaimed:
            blocks.append(f"gap down {abs(gap):.1f} ATR -- wait for a VWAP reclaim")
    if checks.get("band_width_pct") is not None and checks["band_width_pct"] <= cfg.min_band_pct + 0.01:
        blocks.append(f"price band only {checks['band_width_pct']:g}%")

    if t < _hhmm(cfg.entry_start):
        out["stage"] = "watching"
        out["reasons"].append(f"no entries before {cfg.entry_start}")
        return out
    if t >= _hhmm(cfg.entry_end):
        out["stage"] = "closed"
        out["reasons"].append(f"no new entries after {cfg.entry_end}")
        return out

    stage = "watching"
    if stretched:
        stage = "stretched"
    if stretched and exhausted:
        stage = "armed"
    valid = bars_since is not None and bars_since < cfg.entry_valid_bars
    if stage == "armed" and kind and valid:
        stage = "entry"
    if blocks:
        out["stage"] = "blocked" if stage != "watching" else "watching"
        out["reasons"].extend(blocks)
        if stage == "watching":
            out["blocked_reasons"] = blocks
        return out
    out["stage"] = stage

    if stage == "entry":
        entry = round_tick(bars[ci]["close"])
        buffer = max(TICK, price * 0.0005)
        stop = floor_tick(flush["low"] - buffer)
        target = round_tick(vw)
        cost = entry * cfg.cost_pct / 100
        # liquidity cap: this time of day's normal 5-minute traded value (the
        # midday lull is thinner than the open), else the first-hour average
        slot = bars[ci]["ts"].replace(minute=bars[ci]["ts"].minute - bars[ci]["ts"].minute % 5).strftime("%H:%M")
        slot_value = (ctx.get("value_baseline") or {}).get(slot) or ctx.get("avg_5m_value") or 0.0
        size = size_trade(entry, stop, cfg, (one_r if one_r is not None else cfg.one_r) * size_mult,
                          slot_value, exposure_left)
        rps = size["risk_per_share"]
        net_rr = (target - entry - cost) / rps if rps > 0 else 0.0
        fails = []
        if price > entry + 0.25 * max(target - entry, 0):
            fails.append("price already ran from the entry -- let it go")
        if net_rr < cfg.min_net_rr:
            fails.append(f"net R:R {net_rr:.2f} under {cfg.min_net_rr:g}")
        if checks.get("spread_pct") is not None and checks["spread_pct"] > cfg.max_spread_pct:
            fails.append(f"spread {checks['spread_pct']:.2f}% over {cfg.max_spread_pct:g}%")
        if checks.get("lower_band_pct") is not None and checks["lower_band_pct"] < cfg.band_no_entry_pct:
            fails.append(f"{checks['lower_band_pct']:.1f}% from the lower circuit")
        if size["qty"] < 1:
            fails.append("size rounds to zero shares")
        out["suggestion"] = {
            "entry": entry, "stop": stop, "target": target, "qty": size["qty"],
            "risk_amount": size["risk_amount"], "risk_per_share": rps, "notional": size["notional"],
            "caps": size["caps"], "binding_cap": size["binding"], "net_rr": round(net_rr, 2),
            "valid_bars_left": cfg.entry_valid_bars - bars_since, "ok": not fails, "fails": fails,
            "signal_bar": bars[ci]["ts"].strftime("%H:%M"),
        }
    return out


# ------------------------------------------------------------ in a trade

def manage_position(position: dict, bars_1m: list[dict], ev: dict, nifty: dict | None,
                    cfg: HVConfig, now: datetime) -> dict:
    """Prompts for an open long. position: {avg_entry, open_qty, entry_qty,
    entry_fills, planned_stop, risk_amount, opened_at (datetime)}."""
    bars = closed_bars(bars_1m, now)
    m = ev.get("metrics") or {}
    price = m.get("price") or (bars[-1]["close"] if bars else None)
    entry, stop, qty = position["avg_entry"], position.get("planned_stop"), position["open_qty"]
    prompts, warnings = [], []
    out = {"prompts": prompts, "warnings": warnings}
    if not price or not entry:
        return out
    cost = entry * cfg.cost_pct / 100
    rps = (entry - stop + cost) if stop else None
    r_now = (price - entry - cost) / rps if rps and rps > 0 else None
    out["r_now"] = round(r_now, 2) if r_now is not None else None
    out["stop_dist_pct"] = round((price - stop) / price * 100, 2) if stop else None
    vw = m.get("vwap")
    out["to_target_pct"] = round((vw - price) / price * 100, 2) if vw else None

    if r_now is not None and r_now >= 1:
        prompts.append({"kind": "move_stop", "text": f"At +1R: move the stop to {round_tick(entry + cost):.2f} (entry + costs)"})
    if vw and price >= vw and position["open_qty"] >= position["entry_qty"]:
        prompts.append({"kind": "sell_half", "qty": max(1, qty // 2),
                        "text": f"At VWAP: sell half ({max(1, qty // 2)} shares)"})
    five = closed_bars(resample(bars), now, 5)
    if len(five) >= 3:
        a, b, c = five[-3], five[-2], five[-1]
        if b["low"] > a["low"] and b["low"] <= c["low"] and position["open_qty"] < position["entry_qty"]:
            prompts.append({"kind": "trail", "text": f"Trail the stop under the new higher low: {floor_tick(b['low'] - TICK):.2f}"})
    if position.get("entry_fills", 1) == 1 and stop and r_now is not None and r_now < 0.5 and position.get("opened_at"):
        # A pullback that holds above the trade's low: a 1-minute swing low
        # (lower than the bar before, not above the bar after) after the low.
        lows = [x["low"] for x in bars if x["ts"] >= position["opened_at"]]
        if len(lows) >= 4:
            k0 = lows.index(min(lows))
            swing = any(lows[k] > lows[k0] and lows[k] < lows[k - 1] and lows[k] <= lows[k + 1]
                        for k in range(k0 + 2, len(lows) - 1))
            if swing and lows[k0] > stop:
                prompts.append({"kind": "add", "text": "Higher low formed: the second leg is allowed, sized so the total still risks at most 1R"})

    if five:
        last = five[-1]
        rng = last["high"] - last["low"]
        upper_wick = (last["high"] - max(last["open"], last["close"])) / rng if rng else 0
        if vw and last["high"] >= vw > last["close"] and upper_wick >= 0.5 and (m.get("rsi") or 0) > cfg.rsi_exit:
            warnings.append("Rejection at VWAP with RSI(5) over 70 -- exit")
    if nifty and nifty.get("state") == "Trend down":
        warnings.append("Nifty is making new lows -- consider exiting")
    lb = (ev.get("checks") or {}).get("lower_band_pct")
    if lb is not None and lb < cfg.band_exit_pct:
        warnings.append(f"Only {lb:.1f}% from the lower circuit -- exit now")
    opened = position.get("opened_at")
    if opened:
        mins = (now - opened).total_seconds() / 60
        out["minutes_in"] = int(mins)
        out["time_stop_left"] = max(0, int(cfg.time_stop_minutes - mins))
        if mins >= cfg.time_stop_minutes and (r_now is None or r_now < 0.5):
            warnings.append(f"Time stop: not at +0.5R after {cfg.time_stop_minutes} minutes -- exit")
    flat = _hhmm(cfg.flat_by)
    if now.time() >= flat:
        warnings.append(f"It's {cfg.flat_by}: exit everything now")
    elif now.time() >= (datetime.combine(now.date(), flat) - timedelta(minutes=5)).time():
        warnings.append(f"Flat by {cfg.flat_by}: 5 minutes left")
    return out


# --------------------------------------------------------------- scoring

def score_signal(sig: dict, bars_1m: list[dict], cfg: HVConfig) -> dict:
    """What price did after an entry signal. sig: {signal_at (datetime, the
    end of the confirming bar), entry, stop, target}. Fills if price trades
    at the limit within entry_valid_bars; then target vs stop (both in one
    bar counts as stop) within time_stop_minutes, else time-out at the last
    close. R uses the same cost assumption as the card."""
    entry, stop, target = sig["entry"], sig["stop"], sig["target"]
    cost = entry * cfg.cost_pct / 100
    rps = entry - stop + cost
    after = sorted((b for b in bars_1m if b["ts"] >= sig["signal_at"]), key=lambda b: b["ts"])
    if rps <= 0 or not after:
        return {"outcome": None}
    fill_i = next((i for i, b in enumerate(after[:cfg.entry_valid_bars]) if b["low"] <= entry), None)
    if fill_i is None:
        return {"outcome": "not_filled", "outcome_r": None, "mfe_r": None, "mae_r": None}
    end = after[fill_i]["ts"] + timedelta(minutes=cfg.time_stop_minutes)
    mfe = mae = 0.0
    window = [b for b in after[fill_i:] if b["ts"] < end]
    for b in window:
        mfe = max(mfe, (b["high"] - entry) / rps)
        mae = min(mae, (b["low"] - entry) / rps)
        if b["low"] <= stop:
            return {"outcome": "stop", "outcome_r": -1.0, "mfe_r": round(mfe, 2), "mae_r": round(mae, 2)}
        if b["high"] >= target:
            return {"outcome": "target", "outcome_r": round((target - entry - cost) / rps, 2),
                    "mfe_r": round(mfe, 2), "mae_r": round(mae, 2)}
    if not window or window[-1]["ts"] + timedelta(minutes=1) < end:
        return {"outcome": None}  # the 30 minutes haven't passed yet
    last = window[-1]["close"]
    return {"outcome": "timeout", "outcome_r": round((last - entry - cost) / rps, 2),
            "mfe_r": round(mfe, 2), "mae_r": round(mae, 2)}


# ---------------------------------------------------------------- replay

def replay_day(bars_1m: list[dict], ctx: dict, nifty_1m: list[dict], cfg: HVConfig) -> list[dict]:
    """Every entry signal the live tab would have shown on one past day
    (first per episode), scored. No quote, so spread/band checks are
    skipped; sizing uses cfg.one_r."""
    day_bars = sorted(bars_1m, key=lambda b: b["ts"])
    if not day_bars:
        return []
    d = day_bars[0]["ts"].date()
    start = datetime.combine(d, _hhmm(cfg.entry_start))
    stop_at = datetime.combine(d, _hhmm(cfg.entry_end))
    seen, signals = set(), []
    for i, b in enumerate(day_bars):
        now = b["ts"] + timedelta(minutes=1)
        if now < start or now > stop_at:
            continue
        upto = day_bars[:i + 1]
        nifty = nifty_state([x for x in nifty_1m if x["ts"] < now], now) if nifty_1m else None
        ev = evaluate(upto, ctx, None, nifty, cfg, now)
        s = ev["suggestion"]
        if ev["stage"] != "entry" or not s or not s["ok"] or ev["episode"] in seen:
            continue
        seen.add(ev["episode"])
        sig = {"signal_at": now, "entry": s["entry"], "stop": s["stop"], "target": s["target"]}
        signals.append({**sig, "episode": ev["episode"], "qty": s["qty"], "day_type": ev["day_type"]["state"],
                        "metrics": ev["metrics"], **score_signal(sig, day_bars, cfg)})
    return signals


# --------------------------------------------------------------- summary

def signal_summary(signals: list[dict]) -> dict:
    """Hit rate and average R of stored entry signals, grouped by what was
    done with them (taken / skipped / lapsed, or replay), by confirmation
    type and by day type. Only target / stop / time-out count as results."""
    def group(rows):
        done = [r for r in rows if r.get("outcome") in ("target", "stop", "timeout")]
        targets = sum(1 for r in done if r["outcome"] == "target")
        return {
            "signals": len(rows),
            "scored": len(done),
            "not_filled": sum(1 for r in rows if r.get("outcome") == "not_filled"),
            "targets": targets,
            "stops": sum(1 for r in done if r["outcome"] == "stop"),
            "timeouts": sum(1 for r in done if r["outcome"] == "timeout"),
            "hit_rate": round(targets / len(done) * 100, 1) if done else None,
            "avg_r": round(sum(r["outcome_r"] for r in done) / len(done), 2) if done else None,
        }

    entries = [s for s in signals if s.get("stage") == "entry"]
    by_action: dict[str, list] = {}
    for s in entries:
        key = "replay" if s.get("source") == "replay" else (s.get("action") or "pending")
        by_action.setdefault(key, []).append(s)
    by_conf: dict[str, list] = {}
    by_day: dict[str, list] = {}
    for s in entries:
        by_conf.setdefault((s.get("metrics") or {}).get("confirmation") or "unknown", []).append(s)
        by_day.setdefault(s.get("day_type") or "unknown", []).append(s)
    armed = [s for s in signals if s.get("stage") == "armed" and s.get("source", "live") == "live"]
    confirmed = sum(1 for s in armed if s.get("outcome") == "confirmed")
    decided = sum(1 for s in armed if s.get("outcome") in ("confirmed", "no_confirm"))
    return {
        "by_action": {k: group(v) for k, v in by_action.items()},
        "by_confirmation": {k: group(v) for k, v in by_conf.items()},
        "by_day_type": {k: group(v) for k, v in by_day.items()},
        "armed": {"count": len(armed), "confirmed": confirmed,
                  "confirm_rate": round(confirmed / decided * 100, 1) if decided else None},
    }
