"""
Turns a list of signal events (this run's, or everything persisted for a
signal_config_hash) into the summary numbers the page shows: per-horizon
hit rate and mean excess return vs. the tier control, plus a plain-English
verdict. Kept separate from db_store.py -- this is pure arithmetic on
already-fetched rows, no I/O of its own.
"""

import math

HORIZONS = (3, 5, 10, 20)


def _valid_pairs(events: list[dict], horizon: int) -> list[tuple[float, float]]:
    """[(fwd_ret, control_ret), ...] for events where this horizon's
    forward return has actually been observed (not None)."""
    fwd_key, ctrl_key = f"fwd_ret_{horizon}d", f"control_ret_{horizon}d"
    pairs = []
    for e in events:
        fwd, ctrl = e.get(fwd_key), e.get(ctrl_key)
        if fwd is not None:
            pairs.append((float(fwd), float(ctrl) if ctrl is not None else 0.0))
    return pairs


def _bootstrap_ci(values: list[float], n_iter: int = 2000, seed: int = 42) -> tuple[float, float] | None:
    """A crude percentile bootstrap 95% CI on the mean, with no external
    dependency beyond the stdlib -- good enough to say "does the interval
    cross zero", which is all the verdict needs."""
    n = len(values)
    if n < 5:
        return None
    import random
    rng = random.Random(seed)
    means = []
    for _ in range(n_iter):
        sample = [values[rng.randrange(n)] for _ in range(n)]
        means.append(sum(sample) / n)
    means.sort()
    lo_idx = int(0.025 * n_iter)
    hi_idx = int(0.975 * n_iter)
    return means[lo_idx], means[hi_idx]


def horizon_summary(events: list[dict], horizon: int) -> dict:
    pairs = _valid_pairs(events, horizon)
    n = len(pairs)
    if n == 0:
        return {"horizon": horizon, "n": 0, "hit_rate_pct": None, "avg_fwd_ret_pct": None,
                "avg_control_ret_pct": None, "avg_excess_ret_pct": None, "excess_ci": None}

    fwd_rets = [p[0] for p in pairs]
    excess_rets = [p[0] - p[1] for p in pairs]
    hits = sum(1 for r in fwd_rets if r > 0)
    ci = _bootstrap_ci(excess_rets)

    return {
        "horizon": horizon,
        "n": n,
        "hit_rate_pct": round(hits / n * 100, 1),
        "avg_fwd_ret_pct": round(sum(fwd_rets) / n, 2),
        "avg_control_ret_pct": round(sum(p[1] for p in pairs) / n, 2),
        "avg_excess_ret_pct": round(sum(excess_rets) / n, 2),
        "excess_ci": [round(ci[0], 2), round(ci[1], 2)] if ci else None,
    }


def summarize(events: list[dict], horizons: tuple[int, ...] = HORIZONS) -> dict:
    """Full summary block: per-horizon stats plus a one-line verdict based
    on the 10-day (or longest available) horizon's excess-return CI."""
    by_horizon = {h: horizon_summary(events, h) for h in horizons}

    verdict_horizon = 10 if 10 in horizons else max(horizons)
    verdict = by_horizon.get(verdict_horizon, {})
    ci = verdict.get("excess_ci")
    n = verdict.get("n") or 0

    if n < 30:
        verdict_text = (
            f"Only {n} signal(s) with a completed {verdict_horizon}-day forward return -- "
            "too few to draw a conclusion. Run more universe tiers, or wait for more days to elapse."
        )
        verdict_tag = "insufficient_data"
    elif ci is None:
        verdict_text = "Not enough data to compute a confidence interval yet."
        verdict_tag = "insufficient_data"
    elif ci[0] > 0:
        verdict_text = (
            f"Signals beat the universe control by an average of {verdict['avg_excess_ret_pct']:+.2f}% "
            f"over {verdict_horizon} trading days (95% CI {ci[0]:+.2f}% to {ci[1]:+.2f}%, "
            f"n={n}). The confidence interval stays above zero -- there is evidence of an edge."
        )
        verdict_tag = "edge"
    elif ci[1] < 0:
        verdict_text = (
            f"Signals trailed the universe control by an average of {verdict['avg_excess_ret_pct']:+.2f}% "
            f"over {verdict_horizon} trading days (95% CI {ci[0]:+.2f}% to {ci[1]:+.2f}%, "
            f"n={n}). This looks like exhaustion rather than momentum."
        )
        verdict_tag = "no_edge"
    else:
        verdict_text = (
            f"Average excess return over {verdict_horizon} trading days is {verdict['avg_excess_ret_pct']:+.2f}% "
            f"but the 95% confidence interval ({ci[0]:+.2f}% to {ci[1]:+.2f}%, n={n}) spans zero -- "
            "no reliable edge found yet."
        )
        verdict_tag = "no_edge"

    return {
        "by_horizon": by_horizon,
        "verdict_tag": verdict_tag,
        "verdict_text": verdict_text,
        "n_events": len(events),
    }
