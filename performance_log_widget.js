/*
Reusable "Log Trade" widget, shared across every strategy page (ATRx
Screener, ATRx Stock, and any future strategy). Injects its own scoped
styles and a single modal, so a page just needs:

  <script src="/performance-log-widget.js"></script>
  <script>PerformanceLog.init({ strategy: "atrx_screener" });</script>

and, per candidate row: PerformanceLog.open({ symbol, price, meta }).

Styles are self-contained with hardcoded colors (not the host page's CSS
variables) because index.html/atrx_stock.html (--positive/--negative) and
cost_basis.html (--green/--red, slightly different hex shades) already
disagree -- the widget needs to look the same everywhere it's embedded,
regardless of which page's variables happen to be in scope.
*/
(function () {
  "use strict";

  const COLORS = {
    bg: "#10131A", surface: "#171B24", border: "#262B38",
    text: "#F7F8FA", textDim: "#B7BECE", accent: "#E0A857",
    positive: "#4CAF7D", negative: "#E1615B",
  };

  let strategySlug = "unknown_strategy";
  let overlay, strategyInput, symbolInput, qtyInput, entryDateInput, entryPriceInput, notesInput, statusEl, submitBtn;
  let currentMeta = null;
  let onSuccess = null;

  function injectStyles() {
    if (document.getElementById("plw-styles")) return;
    const style = document.createElement("style");
    style.id = "plw-styles";
    style.textContent = `
      .plw-overlay {
        position: fixed; inset: 0; background: rgba(0,0,0,0.6);
        display: none; align-items: center; justify-content: center; z-index: 1000;
        font-family: 'Space Grotesk', sans-serif;
      }
      .plw-overlay.plw-open { display: flex; }
      .plw-card {
        background: ${COLORS.surface}; border: 1px solid ${COLORS.border}; border-radius: 8px;
        padding: 24px; width: 360px; max-width: 90vw; color: ${COLORS.text};
        box-shadow: 0 12px 40px rgba(0,0,0,0.5);
      }
      .plw-card h3 { margin: 0 0 4px; font-size: 16px; font-weight: 600; }
      .plw-card .plw-sub { color: ${COLORS.textDim}; font-size: 12px; margin: 0 0 16px; line-height: 1.4; }
      .plw-field { display: flex; flex-direction: column; gap: 5px; margin-bottom: 12px; }
      .plw-field label { font-size: 11.5px; color: ${COLORS.textDim}; }
      .plw-field input, .plw-field textarea {
        background: ${COLORS.bg}; border: 1px solid ${COLORS.border}; border-radius: 4px;
        color: ${COLORS.text}; font-family: 'IBM Plex Mono', monospace; font-size: 13px;
        padding: 7px 9px; width: 100%; box-sizing: border-box; resize: vertical;
      }
      .plw-field input:focus, .plw-field textarea:focus { outline: none; border-color: ${COLORS.accent}; }
      .plw-field input:disabled { opacity: 0.6; }
      .plw-actions { display: flex; gap: 10px; margin-top: 18px; }
      .plw-actions button {
        border: none; border-radius: 4px; padding: 9px 16px; font-family: 'Space Grotesk', sans-serif;
        font-weight: 600; font-size: 13px; cursor: pointer;
      }
      .plw-btn-primary { background: ${COLORS.accent}; color: ${COLORS.bg}; }
      .plw-btn-secondary { background: transparent !important; color: ${COLORS.textDim}; border: 1px solid ${COLORS.border} !important; }
      .plw-actions button:disabled { opacity: 0.5; cursor: default; }
      .plw-status { font-size: 12px; margin-top: 10px; padding: 8px 10px; border-radius: 4px; display: none; }
      .plw-status.plw-show { display: block; }
      .plw-status.plw-error { background: rgba(225,97,91,0.12); color: ${COLORS.negative}; border: 1px solid ${COLORS.negative}; }
      .plw-status.plw-ok { background: rgba(76,175,125,0.12); color: ${COLORS.positive}; border: 1px solid ${COLORS.positive}; }
    `;
    document.head.appendChild(style);
  }

  function buildModal() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.className = "plw-overlay";
    overlay.innerHTML = `
      <div class="plw-card">
        <h3>Log Trade</h3>
        <p class="plw-sub">Records a position in the performance log -- fill in the entry now, come back to close it once you exit.</p>
        <div class="plw-field"><label>Strategy</label><input type="text" id="plw-strategy"></div>
        <div class="plw-field"><label>Symbol</label><input type="text" id="plw-symbol"></div>
        <div class="plw-field"><label>Qty</label><input type="number" id="plw-qty" min="1" step="1"></div>
        <div class="plw-field"><label>Entry date</label><input type="date" id="plw-entry-date"></div>
        <div class="plw-field"><label>Entry price</label><input type="number" id="plw-entry-price" step="0.01" min="0"></div>
        <div class="plw-field"><label>Notes (optional)</label><textarea id="plw-notes" rows="2"></textarea></div>
        <div class="plw-actions">
          <button type="button" class="plw-btn-primary" id="plw-submit">Log Trade</button>
          <button type="button" class="plw-btn-secondary" id="plw-cancel">Cancel</button>
        </div>
        <div class="plw-status" id="plw-status"></div>
      </div>
    `;
    document.body.appendChild(overlay);

    strategyInput = overlay.querySelector("#plw-strategy");
    symbolInput = overlay.querySelector("#plw-symbol");
    qtyInput = overlay.querySelector("#plw-qty");
    entryDateInput = overlay.querySelector("#plw-entry-date");
    entryPriceInput = overlay.querySelector("#plw-entry-price");
    notesInput = overlay.querySelector("#plw-notes");
    statusEl = overlay.querySelector("#plw-status");
    submitBtn = overlay.querySelector("#plw-submit");

    overlay.querySelector("#plw-cancel").addEventListener("click", close);
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    submitBtn.addEventListener("click", submit);
  }

  function setStatus(kind, message) {
    statusEl.className = "plw-status plw-show" + (kind ? " plw-" + kind : "");
    statusEl.textContent = message;
  }

  function clearStatus() {
    statusEl.className = "plw-status";
    statusEl.textContent = "";
  }

  function close() {
    overlay.classList.remove("plw-open");
  }

  function todayIso() {
    return new Date().toISOString().slice(0, 10);
  }

  async function submit() {
    const strategy = strategyInput.value.trim();
    const symbol = symbolInput.value.trim().toUpperCase();
    const qty = parseInt(qtyInput.value, 10);
    const entryDate = entryDateInput.value;
    const entryPrice = parseFloat(entryPriceInput.value);
    const notes = notesInput.value.trim() || null;

    if (!strategy || !symbol || !qty || qty <= 0 || !entryDate || !entryPrice || entryPrice <= 0) {
      setStatus("error", "Strategy, symbol, qty, entry date, and entry price are all required.");
      return;
    }

    submitBtn.disabled = true;
    submitBtn.textContent = "Logging…";
    clearStatus();

    try {
      const res = await fetch("/api/trades", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          strategy, symbol, qty,
          entry_date: entryDate, entry_price: entryPrice,
          notes, signal_meta: currentMeta,
        }),
      });
      const data = await res.json();
      if (!res.ok) {
        setStatus("error", data.error || "Failed to log trade.");
        return;
      }
      setStatus("ok", "Trade logged.");
      if (typeof onSuccess === "function") onSuccess();
      setTimeout(close, 700);
    } catch (e) {
      setStatus("error", "Request failed: " + e.message);
    } finally {
      submitBtn.disabled = false;
      submitBtn.textContent = "Log Trade";
    }
  }

  window.PerformanceLog = {
    init(opts) {
      strategySlug = (opts && opts.strategy) || "unknown_strategy";
    },
    open(opts) {
      opts = opts || {};
      injectStyles();
      buildModal();
      clearStatus();
      submitBtn.disabled = false;
      submitBtn.textContent = "Log Trade";

      strategyInput.value = strategySlug;
      strategyInput.disabled = !!opts.symbol;
      symbolInput.value = opts.symbol || "";
      symbolInput.disabled = !!opts.symbol;
      qtyInput.value = "";
      entryDateInput.value = todayIso();
      entryPriceInput.value = opts.price != null ? opts.price : "";
      notesInput.value = "";
      currentMeta = opts.meta || null;
      onSuccess = typeof opts.onSuccess === "function" ? opts.onSuccess : null;

      overlay.classList.add("plw-open");
      (opts.symbol ? qtyInput : symbolInput).focus();
    },
  };
})();
