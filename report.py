"""
Builds a single, self-contained HTML file from the screener's output so it
can be opened, sorted, and filtered without re-running anything. All data
is embedded inline as JSON; sorting/filtering happens client-side in
vanilla JS, no server or network dependency at view time (aside from the
two Google Fonts links, which degrade gracefully to system fonts offline).
"""

import json
import logging
from datetime import datetime

from config import CONFIG

logger = logging.getLogger("atrx.report")


def generate_html_report(rows: list[dict], universe_size: int, volatile_count: int) -> str:
    """
    rows: the FULL scored+sorted candidate list (not truncated to top_n) --
    this report is meant for you to filter yourself, so it shouldn't hide
    candidates the CSV export leaves out.
    Returns the path to the written HTML file.
    """
    generated_at = datetime.now()
    data_json = json.dumps(rows)

    html = _TEMPLATE.format(
        generated_at=generated_at.strftime("%d %b %Y, %H:%M"),
        universe_size=universe_size,
        volatile_count=volatile_count,
        candidate_count=len(rows),
        data_json=data_json,
        atrx_lower=CONFIG.atrx_lower,
        atrx_upper=CONFIG.atrx_upper,
    )

    CONFIG.output_dir.mkdir(parents=True, exist_ok=True)
    dated_path = CONFIG.output_dir / f"atrx_report_{generated_at:%Y%m%d_%H%M}.html"
    latest_path = CONFIG.output_dir / "atrx_report_latest.html"

    try:
        dated_path.write_text(html, encoding="utf-8")
        latest_path.write_text(html, encoding="utf-8")
    except OSError as e:
        logger.error("Could not write HTML report: %s", e)
        raise

    return str(latest_path)


_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ATRx Screener</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;500;600;700&family=IBM+Plex+Mono:wght@400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --bg: #10131A;
    --surface: #171B24;
    --surface-alt: #1D2230;
    --border: #262B38;
    --text: #E7E9EE;
    --text-dim: #8B93A7;
    --accent: #E0A857;
    --positive: #4CAF7D;
    --negative: #E1615B;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--bg);
    color: var(--text);
    font-family: 'Space Grotesk', sans-serif;
    -webkit-font-smoothing: antialiased;
  }}
  .num {{ font-family: 'IBM Plex Mono', monospace; font-variant-numeric: tabular-nums; }}

  header {{
    padding: 28px 32px 20px;
    border-bottom: 1px solid var(--border);
  }}
  header h1 {{
    margin: 0 0 4px;
    font-size: 22px;
    font-weight: 600;
    letter-spacing: -0.01em;
  }}
  header .meta {{
    color: var(--text-dim);
    font-size: 13px;
  }}
  header .meta .num {{ color: var(--text); }}

  .filters {{
    position: sticky;
    top: 0;
    z-index: 10;
    display: flex;
    flex-wrap: wrap;
    gap: 14px;
    align-items: flex-end;
    padding: 16px 32px;
    background: var(--surface);
    border-bottom: 1px solid var(--border);
  }}
  .field {{ display: flex; flex-direction: column; gap: 5px; }}
  .field label {{
    font-size: 11px;
    color: var(--text-dim);
  }}
  .field input {{
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 4px;
    color: var(--text);
    font-family: 'IBM Plex Mono', monospace;
    font-size: 13px;
    padding: 7px 9px;
    width: 92px;
  }}
  .field input#search {{ width: 150px; }}
  .field input:focus {{
    outline: none;
    border-color: var(--accent);
  }}
  #reset {{
    background: transparent;
    border: 1px solid var(--border);
    color: var(--text-dim);
    border-radius: 4px;
    padding: 8px 14px;
    font-family: 'Space Grotesk', sans-serif;
    font-size: 13px;
    cursor: pointer;
    height: 33px;
  }}
  #reset:hover {{ border-color: var(--accent); color: var(--accent); }}
  #count {{
    margin-left: auto;
    align-self: center;
    color: var(--text-dim);
    font-size: 13px;
  }}
  #count .num {{ color: var(--accent); }}

  .table-wrap {{ overflow-x: auto; padding: 0 32px 40px; }}
  table {{ width: 100%; border-collapse: collapse; margin-top: 18px; min-width: 1080px; }}
  th {{
    text-align: right;
    font-size: 11px;
    font-weight: 500;
    color: var(--text-dim);
    padding: 8px 12px;
    border-bottom: 1px solid var(--border);
    cursor: pointer;
    white-space: nowrap;
    user-select: none;
  }}
  th:first-child, td:first-child {{ text-align: left; }}
  th:hover {{ color: var(--accent); }}
  th.sorted {{ color: var(--accent); }}
  th .arrow {{ font-size: 9px; margin-left: 3px; }}

  td {{
    padding: 9px 12px;
    border-bottom: 1px solid var(--border);
    font-size: 13px;
    text-align: right;
    white-space: nowrap;
  }}
  tbody tr:hover {{ background: var(--surface-alt); }}
  td.symbol {{ font-weight: 600; text-align: left; }}

  .pos {{ color: var(--positive); }}
  .neg {{ color: var(--negative); }}
  .breach-flag {{ color: var(--negative); }}

  .gauge {{
    display: inline-block;
    width: 64px;
    height: 6px;
    background: var(--border);
    border-radius: 3px;
    position: relative;
    vertical-align: middle;
    margin-right: 8px;
  }}
  .gauge .marker {{
    position: absolute;
    top: -3px;
    width: 2px;
    height: 12px;
    background: var(--accent);
    border-radius: 1px;
  }}
  .gauge .zero {{
    position: absolute;
    top: 0;
    width: 1px;
    height: 6px;
    background: var(--text-dim);
    opacity: 0.5;
  }}

  footer {{
    padding: 20px 32px 32px;
    color: var(--text-dim);
    font-size: 12px;
    line-height: 1.6;
    max-width: 720px;
  }}
</style>
</head>
<body>

<header>
  <h1>ATRx Screener</h1>
  <div class="meta">
    Generated <span class="num">{generated_at}</span>
    &nbsp;·&nbsp; <span class="num">{universe_size}</span> symbols scanned
    &nbsp;·&nbsp; <span class="num">{volatile_count}</span> passed the volatility filter
    &nbsp;·&nbsp; <span class="num">{candidate_count}</span> candidates below
  </div>
</header>

<div class="filters">
  <div class="field">
    <label for="search">Symbol</label>
    <input id="search" type="text" placeholder="e.g. RELIANCE">
  </div>
  <div class="field">
    <label for="minScore">Min score</label>
    <input id="minScore" type="number" step="0.01" placeholder="any">
  </div>
  <div class="field">
    <label for="atrxMin">ATRx min</label>
    <input id="atrxMin" type="number" step="0.1" placeholder="{atrx_lower}">
  </div>
  <div class="field">
    <label for="atrxMax">ATRx max</label>
    <input id="atrxMax" type="number" step="0.1" placeholder="{atrx_upper}">
  </div>
  <div class="field">
    <label for="minTouches">Min touches</label>
    <input id="minTouches" type="number" step="1" placeholder="any">
  </div>
  <div class="field">
    <label for="maxBreaches">Max breaches</label>
    <input id="maxBreaches" type="number" step="1" placeholder="any">
  </div>
  <div class="field">
    <label for="minBacktest">Min backtest n</label>
    <input id="minBacktest" type="number" step="1" placeholder="any">
  </div>
  <button id="reset">Reset filters</button>
  <div id="count"></div>
</div>

<div class="table-wrap">
  <table>
    <thead>
      <tr id="headerRow"></tr>
    </thead>
    <tbody id="tbody"></tbody>
  </table>
</div>

<footer>
  Score, ATRx, touches, breaches and the backtest columns are explained in
  README.md. This report is a snapshot from one screener run against daily
  candles — it does not update intraday. Nothing here is a recommendation
  to trade; review the chart and the sample size behind any statistic
  (backtest n) before acting on it.
</footer>

<script>
const DATA = {data_json};
const ATRX_LOWER = {atrx_lower};
const ATRX_UPPER = {atrx_upper};

const COLUMNS = [
  {{ key: "symbol", label: "Symbol" }},
  {{ key: "current_price", label: "Price" }},
  {{ key: "level", label: "Level" }},
  {{ key: "atrx", label: "ATRx" }},
  {{ key: "atr_pct", label: "ATR%" }},
  {{ key: "touches", label: "Touches" }},
  {{ key: "breaches", label: "Breach" }},
  {{ key: "recency_weight", label: "Recency" }},
  {{ key: "backtest_touches", label: "BT n" }},
  {{ key: "hit_rate_5d_pct", label: "Hit% 5d" }},
  {{ key: "avg_fwd_ret_5d_pct", label: "AvgRet% 5d" }},
  {{ key: "score", label: "Score" }},
];

let sortKey = "score";
let sortDir = -1; // -1 = desc

function renderHeader() {{
  const row = document.getElementById("headerRow");
  row.innerHTML = "";
  COLUMNS.forEach(col => {{
    const th = document.createElement("th");
    th.textContent = col.label;
    if (col.key === sortKey) {{
      th.classList.add("sorted");
      const arrow = document.createElement("span");
      arrow.className = "arrow";
      arrow.textContent = sortDir === 1 ? "\\u25B2" : "\\u25BC";
      th.appendChild(arrow);
    }}
    th.onclick = () => {{
      if (sortKey === col.key) {{ sortDir *= -1; }}
      else {{ sortKey = col.key; sortDir = col.key === "symbol" ? 1 : -1; }}
      renderHeader();
      renderRows();
    }};
    row.appendChild(th);
  }});
}}

function gaugeHtml(atrx) {{
  const range = ATRX_UPPER - ATRX_LOWER;
  const pct = Math.max(0, Math.min(1, (atrx - ATRX_LOWER) / range));
  const zeroPct = Math.max(0, Math.min(1, (0 - ATRX_LOWER) / range));
  return `<span class="gauge">
    <span class="zero" style="left:${{zeroPct * 100}}%"></span>
    <span class="marker" style="left:${{pct * 100}}%"></span>
  </span>`;
}}

function fmt(val, decimals) {{
  if (val === null || val === undefined) return "\\u2013";
  return Number(val).toFixed(decimals);
}}

function getFilters() {{
  return {{
    search: document.getElementById("search").value.trim().toUpperCase(),
    minScore: parseFloat(document.getElementById("minScore").value),
    atrxMin: parseFloat(document.getElementById("atrxMin").value),
    atrxMax: parseFloat(document.getElementById("atrxMax").value),
    minTouches: parseFloat(document.getElementById("minTouches").value),
    maxBreaches: parseFloat(document.getElementById("maxBreaches").value),
    minBacktest: parseFloat(document.getElementById("minBacktest").value),
  }};
}}

function applyFilters(rows) {{
  const f = getFilters();
  return rows.filter(r => {{
    if (f.search && !r.symbol.toUpperCase().includes(f.search)) return false;
    if (!isNaN(f.minScore) && r.score < f.minScore) return false;
    if (!isNaN(f.atrxMin) && r.atrx < f.atrxMin) return false;
    if (!isNaN(f.atrxMax) && r.atrx > f.atrxMax) return false;
    if (!isNaN(f.minTouches) && r.touches < f.minTouches) return false;
    if (!isNaN(f.maxBreaches) && r.breaches > f.maxBreaches) return false;
    if (!isNaN(f.minBacktest) && r.backtest_touches < f.minBacktest) return false;
    return true;
  }});
}}

function renderRows() {{
  let rows = applyFilters(DATA);
  rows.sort((a, b) => {{
    const av = a[sortKey], bv = b[sortKey];
    if (typeof av === "string") return sortDir * av.localeCompare(bv);
    return sortDir * ((av ?? -Infinity) - (bv ?? -Infinity));
  }});

  const tbody = document.getElementById("tbody");
  tbody.innerHTML = "";
  rows.forEach(r => {{
    const tr = document.createElement("tr");
    const scoreClass = r.score >= 0 ? "pos" : "neg";
    const retClass = (r.avg_fwd_ret_5d_pct ?? 0) >= 0 ? "pos" : "neg";
    const breachClass = r.breaches > 0 ? "breach-flag" : "";
    tr.innerHTML = `
      <td class="symbol">${{r.symbol}}</td>
      <td class="num">${{fmt(r.current_price, 2)}}</td>
      <td class="num">${{fmt(r.level, 2)}}</td>
      <td class="num">${{gaugeHtml(r.atrx)}}${{fmt(r.atrx, 2)}}</td>
      <td class="num">${{fmt(r.atr_pct, 2)}}%</td>
      <td class="num">${{r.touches}}</td>
      <td class="num ${{breachClass}}">${{r.breaches}}</td>
      <td class="num">${{fmt(r.recency_weight, 2)}}</td>
      <td class="num">${{r.backtest_touches}}</td>
      <td class="num">${{fmt(r.hit_rate_5d_pct, 1)}}${{r.hit_rate_5d_pct != null ? "%" : ""}}</td>
      <td class="num ${{retClass}}">${{fmt(r.avg_fwd_ret_5d_pct, 2)}}${{r.avg_fwd_ret_5d_pct != null ? "%" : ""}}</td>
      <td class="num ${{scoreClass}}">${{fmt(r.score, 3)}}</td>
    `;
    tbody.appendChild(tr);
  }});
  document.getElementById("count").innerHTML =
    `showing <span class="num">${{rows.length}}</span> of <span class="num">${{DATA.length}}</span>`;
}}

document.querySelectorAll(".filters input").forEach(el => el.addEventListener("input", renderRows));
document.getElementById("reset").addEventListener("click", () => {{
  document.querySelectorAll(".filters input").forEach(el => el.value = "");
  renderRows();
}});

renderHeader();
renderRows();
</script>
</body>
</html>
"""
