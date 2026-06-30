// Shared helpers for every dashboard page.
// Loaded as <script src="/app/_shared.js"></script> before each page's
// inline <script>. Exposes everything as globals — kept simple on purpose
// (no modules, no bundler, no build step).
//
// In addition to formatting helpers, this file owns the **shared chrome**:
//   - the "🔄 Actualizar" button + "⚙ Cuenta" button in the subtitle
//   - the staleness chip next to "Última actualización"
//   - the TOTP modal
//   - the GBM+ credentials (config) modal
//   - the progress overlay shown during /update
//   - the keyboard handler (Escape closes modals)
//   - the first-run check that opens the config modal automatically
//
// Pages don't need any of that HTML/CSS — just include this script and
// the chrome is injected on DOMContentLoaded. To run page-specific code
// after a successful update, define `window.onUpdateComplete = async () => {...}`
// (typically the page's `load()` data refresher). If undefined, the page
// is reloaded with `location.reload()` instead.

const VERSION = "0.13.0";

const MONTH_NAMES = [
  "enero", "febrero", "marzo", "abril", "mayo", "junio",
  "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
];

// Format a number as Mexican currency.
//   currency: true       → prepend "$"
//   sign:     true       → prepend "+" for positives (negatives always get "-")
//   decimals: N          → fraction digits (default 2)
function fmtMoney(n, opts = {}) {
  if (n == null || isNaN(n)) return "—";
  const { decimals = 2, currency = false, sign = false } = opts;
  const abs = Math.abs(n);
  const formatted = abs.toLocaleString("es-MX", {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
  const signPrefix = n < 0 ? "-" : (sign && n > 0 ? "+" : "");
  return signPrefix + (currency ? "$" : "") + formatted;
}

function formatDate(iso) {
  if (!iso) return "—";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleDateString("es-MX", { year: "numeric", month: "short", day: "2-digit" });
}

function formatTime(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  if (isNaN(d.getTime())) return "";
  return d.toLocaleTimeString("es-MX", { hour: "2-digit", minute: "2-digit", hour12: true });
}

// Formats the "última actualización" stamp written by fetch_data.py.
// The fetch script writes a naive timestamp (no TZ marker) using the
// server's local clock. To avoid the browser misinterpreting that as
// its own local time (which produced wrong hours when the server is in
// UTC and the user in CEST), we attach 'Z' so it's parsed as UTC, then
// display it in Mexico City time — the timezone of the market and of
// GBM's own apps. Strings that already include a TZ marker (process_date
// from /transactions etc.) are left untouched.
// Returns a "hace N min" / "hace Nh Mm" label + severity hint for a
// timestamp written by the fetch script. Used to make it obvious in
// the UI when the snapshot is older than the user might assume — the
// dashboard is a cached view and the broker's app is live. The Bracket
// chip color follows: green ≤ 15 min, amber ≤ 1 h, red > 1 h.
function stalenessHint(iso) {
  if (!iso) return null;
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso.trim());
  const parseable = hasTz ? iso.trim() : iso.trim().replace(" ", "T") + "Z";
  const d = new Date(parseable);
  if (isNaN(d.getTime())) return null;
  const ageMs = Date.now() - d.getTime();
  const mins = Math.floor(ageMs / 60000);
  let label;
  if (mins < 1) label = "ahora";
  else if (mins < 60) label = `hace ${mins} min`;
  else {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    label = m === 0 ? `hace ${h} h` : `hace ${h} h ${m} min`;
  }
  const severity = mins <= 15 ? "fresh" : mins <= 60 ? "warn" : "stale";
  return { label, severity, ageMinutes: mins };
}

function formatTimestamp(iso) {
  if (!iso) return "—";
  const hasTz = /Z|[+-]\d{2}:?\d{2}$/.test(iso.trim());
  const parseable = hasTz ? iso.trim() : iso.trim().replace(" ", "T") + "Z";
  const d = new Date(parseable);
  if (isNaN(d.getTime())) return iso;
  return d.toLocaleString("es-MX", {
    timeZone: "America/Mexico_City",
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit",
  }) + " CDMX";
}

// Bucket an ISO date string by month, returning "YYYY-MM".
function monthKey(iso) {
  const d = new Date(iso);
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}`;
}

function monthLabel(k) {
  const [y, m] = k.split("-");
  return `${MONTH_NAMES[parseInt(m, 10) - 1]} ${y}`;
}

// ======================================================================
// Shared chrome — update button, modals, progress overlay
// ======================================================================

// CSS for everything injected by this file. Kept in one block so the chrome
// is fully self-contained (no need to duplicate styles in every page).
const SHARED_CHROME_CSS = `
/* ============ Top bar — brand + tabs + actions (TR-style) ============ */
/* Sticky strip at the top of every page. Reaches body edges via negative
   margin (body has padding:24px). No backdrop-filter — Carlos prefers a
   solid bg, same convention as modal scrims. */
.top-bar {
  position: sticky; top: 0; z-index: 60;
  background: var(--bg);
  border-bottom: 1px solid var(--border);
  display: flex; align-items: center; gap: 16px; justify-content: space-between;
  padding: 12px 24px;
  margin: -24px -24px 24px;
}
.top-bar .brand { display: flex; align-items: center; gap: 10px; flex-shrink: 0; }
.top-bar .brand-logo {
  /* Brand-adjacent: navy → teal gradient evokes GBM's corporate look
     (gbm.com) without going full light-theme. White text stays
     readable across the whole strip. */
  background: linear-gradient(135deg, #003b71, #00b8a9);
  width: 28px; height: 28px; border-radius: 6px;
  display: flex; align-items: center; justify-content: center;
  font-size: 11px; font-weight: 800; color: white;
  letter-spacing: 0.5px;
}
.top-bar .brand-title { font-size: 15px; font-weight: 700; color: var(--text); }
.top-bar nav {
  display: flex; gap: 4px; flex: 1; justify-content: center;
  flex-wrap: wrap; min-width: 0;
}
.top-bar nav a {
  color: var(--muted); text-decoration: none; font-size: 13px;
  padding: 7px 14px; border-radius: 8px; font-weight: 500;
  border: 1px solid transparent;
  transition: color 0.15s, background 0.15s, border-color 0.15s;
}
.top-bar nav a:hover { color: var(--text); background: rgba(255,255,255,0.04); }
.top-bar nav a.active {
  color: var(--blue);
  background: rgba(96, 165, 250, 0.10);
  border-color: var(--blue);
}
.top-bar .actions { display: flex; gap: 8px; align-items: center; flex-shrink: 0; }
.top-bar .actions .update-btn {
  background: var(--blue); color: var(--bg); border: none;
  padding: 7px 16px; border-radius: 8px; font-size: 13px; font-weight: 600;
  cursor: pointer; display: inline-flex; align-items: center; gap: 6px;
}
.top-bar .actions .update-btn:hover { background: #93c5fd; }
.top-bar .actions .update-btn:disabled { opacity: 0.7; cursor: progress; }
@media (max-width: 820px) {
  .top-bar { flex-wrap: wrap; gap: 8px; padding: 10px 16px; margin: -24px -24px 16px; }
  .top-bar nav { flex: 1 0 100%; justify-content: flex-start; order: 3; }
  .top-bar .brand-title { display: none; }
}

.staleness-chip {
  display: none; margin-left: 8px;
  padding: 2px 8px; border-radius: 10px;
  font-size: 11px; font-weight: 600;
  vertical-align: middle;
}
.staleness-chip.show { display: inline-block; }
.staleness-chip.fresh { background: rgba(74, 222, 128, 0.15); color: var(--green); }
.staleness-chip.warn  { background: rgba(251, 191, 36, 0.18); color: var(--amber); }
.staleness-chip.stale { background: rgba(248, 113, 113, 0.22); color: var(--red); }

/* modal — used for both TOTP and config */
.modal-backdrop {
  position: fixed; inset: 0; background: rgba(0,0,0,0.65);
  display: none; align-items: center; justify-content: center;
  z-index: 100;
}
.modal-backdrop.show { display: flex; }
.modal {
  background: var(--card); border: 1px solid var(--border);
  border-radius: 12px; padding: 28px; max-width: 420px; width: 90%;
  box-shadow: 0 20px 60px rgba(0,0,0,0.5);
}
.modal h2 { font-size: 18px; margin-bottom: 8px; }
.modal p { color: var(--muted); font-size: 13px; margin-bottom: 20px; line-height: 1.5; }
.modal input.totp {
  width: 100%; background: var(--bg); border: 2px solid var(--border);
  color: var(--text); padding: 14px; border-radius: 8px;
  font-size: 24px; font-family: monospace; text-align: center;
  letter-spacing: 8px; margin-bottom: 16px;
}
.modal input.totp:focus { outline: none; border-color: var(--blue); }
/* Field labels (e.g. "Email", "Contraseña") above each input — small,
   uppercased, muted. Excludes .modal-checkbox (wrapper around a
   checkbox + description sentence — must render as normal prose, the
   upper-case rule was bleeding into it and turned the Recargar-todo
   blurb into ALL-CAPS). NB: no backticks in this comment — the whole
   SHARED_CHROME_CSS is a template literal and stray backticks close
   it prematurely (regression 2026-06-05 → top-bar and update flow
   disappeared from the local dashboard). */
.modal label:not(.modal-checkbox) {
  display: block; font-size: 12px; text-transform: uppercase;
  letter-spacing: 0.7px; color: var(--muted); margin-bottom: 6px;
  margin-top: 14px; font-weight: 600;
}
.modal label:not(.modal-checkbox):first-of-type { margin-top: 0; }
.modal input.field {
  width: 100%; background: var(--bg); border: 1px solid var(--border);
  color: var(--text); padding: 12px 14px; border-radius: 8px;
  font-size: 14px; font-family: -apple-system, sans-serif;
}
.modal input.field:focus { outline: none; border-color: var(--blue); }
.modal .modal-btns { display: flex; gap: 12px; justify-content: flex-end; }
.modal button {
  padding: 10px 20px; border-radius: 8px; border: none;
  font-size: 14px; cursor: pointer; font-weight: 600;
}
.modal button.primary { background: var(--blue); color: var(--bg); }
.modal button.primary:hover { background: #93c5fd; }
.modal button.primary:disabled { opacity: 0.5; cursor: wait; }
.modal button.secondary {
  background: transparent; color: var(--muted); border: 1px solid var(--border);
}
.modal button.secondary:hover { color: var(--text); }
.modal .modal-error {
  background: rgba(248, 113, 113, 0.1); border-left: 3px solid var(--red);
  padding: 10px 14px; border-radius: 6px; font-size: 13px;
  margin-bottom: 16px; color: var(--red);
}
.modal .modal-error.hidden { display: none; }
.modal .modal-hint {
  font-size: 11px; color: var(--muted); margin-top: 12px; text-align: center;
}

/* Non-blocking update flow — ported from Trade-Republic-Dashboard.
   The previous design was a full-viewport overlay that intercepted
   clicks and dimmed the page; Carlos prefers TR's pattern where
   updates surface as a thin progress bar at the top of the viewport
   plus a small toast under the cockpit. Both are non-blocking, so
   the user keeps scrolling/clicking while the fetch runs. */

/* Thin progress bar pinned to the very top of the viewport. */
.progress-bar {
  position: fixed; top: 0; left: 0; right: 0; height: 2px;
  z-index: 100; pointer-events: none;
  display: none;
}
.progress-bar.active { display: block; }
.progress-bar.indet {
  background: linear-gradient(90deg, transparent 0%, var(--blue) 50%, transparent 100%);
  background-size: 50% 100%; background-repeat: no-repeat;
  animation: gbm-pb-slide 1.6s ease-in-out infinite;
}
@keyframes gbm-pb-slide {
  0%   { background-position: -50% 0; }
  100% { background-position: 150% 0; }
}

/* Toast — top-center, under the sticky top-bar. Non-blocking. */
.toast {
  position: fixed; top: 200px; left: 50%; transform: translateX(-50%) translateY(-10px);
  background: var(--card); border: 1px solid var(--border);
  border-top: 3px solid var(--blue);
  padding: 12px 20px; border-radius: 0 0 10px 10px;
  min-width: 320px; max-width: 520px;
  box-shadow: 0 12px 32px rgba(0,0,0,0.5);
  display: none; z-index: 90;
  font-size: 14px; text-align: center;
  opacity: 0; transition: opacity 0.18s, transform 0.18s;
}
.toast.active { display: block; opacity: 1; transform: translateX(-50%) translateY(0); }
.toast .t-title { font-weight: 700; margin-bottom: 4px;
  display: flex; align-items: center; justify-content: center; gap: 10px; }
.toast .t-stage { color: var(--muted); font-size: 12px; }
.toast.ok  { border-top-color: var(--green); }
.toast.err { border-top-color: var(--red);   }
.toast .t-close {
  background: none; border: none; color: var(--muted); cursor: pointer;
  position: absolute; top: 6px; right: 10px; font-size: 16px; padding: 0;
}
.toast .spin {
  width: 14px; height: 14px; display: inline-block;
  border: 2px solid var(--border); border-top-color: var(--blue);
  border-radius: 50%; animation: gbm-spin 0.9s linear infinite;
}
@keyframes gbm-spin { to { transform: rotate(360deg); } }
@media (max-width: 700px) {
  .toast { min-width: 80vw; max-width: 95vw; top: 220px; }
}
`;

// Modal + overlay HTML appended to <body> on every page.
const SHARED_CHROME_HTML = `
<!-- Config modal (GBM email + password) -->
<div class="modal-backdrop" id="config-modal" onclick="closeModalIfBackdrop(event, 'config')">
  <div class="modal" onclick="event.stopPropagation()">
    <h2>⚙ Configuración de cuenta GBM+</h2>
    <p>
      Estas credenciales se guardan en <code>app/.env</code> (permisos
      <code>0600</code>, solo tu usuario las puede leer) y nunca salen de
      tu máquina. Si tienes 2FA activado, el código TOTP se pedirá
      después al actualizar.
    </p>
    <div class="modal-error hidden" id="config-error"></div>
    <label for="config-email">Email</label>
    <input
      type="email"
      class="field"
      id="config-email"
      name="gbm-account-email"
      autocomplete="off"
      placeholder="tu-email@dominio.com"
      oninput="onConfigInput()"
    >
    <label for="config-password">Contraseña</label>
    <input
      type="password"
      class="field"
      id="config-password"
      name="gbm-account-pass"
      autocomplete="new-password"
      placeholder="••••••••"
      oninput="onConfigInput()"
      onkeydown="if (event.key === 'Enter') submitConfig()"
    >
    <div style="height: 20px;"></div>
    <div class="modal-btns">
      <button class="secondary" onclick="closeConfigModal()">Cancelar</button>
      <button class="primary" id="config-submit" onclick="submitConfig()" disabled>Guardar</button>
    </div>
    <div class="modal-hint">
      Todo es local. Cero telemetría. Cero envío fuera de tu computadora.
    </div>
  </div>
</div>

<!-- TOTP modal -->
<div class="modal-backdrop" id="totp-modal" onclick="closeModalIfBackdrop(event)">
  <div class="modal" onclick="event.stopPropagation()">
    <h2>🔐 Código de seguridad</h2>
    <p>
      Tu sesión expiró. Abre tu app autenticadora (Google Authenticator /
      Authy / la que uses) y teclea el código de <b>6 dígitos</b> para GBM+.
    </p>
    <div class="modal-error hidden" id="totp-error"></div>
    <input
      type="text"
      class="totp"
      id="totp-input"
      maxlength="6"
      inputmode="numeric"
      autocomplete="off"
      placeholder="000000"
      oninput="onTotpInput(event)"
      onkeydown="if (event.key === 'Enter') submitTotp()"
    >
    <label class="modal-checkbox" style="display:flex; align-items:flex-start; gap:8px; font-size:12px; color: var(--muted); margin: -4px 0 16px; cursor: pointer; line-height: 1.4;">
      <input type="checkbox" id="totp-full-reload" style="margin-top: 2px;">
      <span>Recargar <b>todo desde cero</b> (descarga lenta — solo cuando cambiaste de cuenta o quieres limpiar datos viejos)</span>
    </label>
    <div class="modal-btns">
      <button class="secondary" onclick="closeModal()">Cancelar</button>
      <button class="primary" id="totp-submit" onclick="submitTotp()">Actualizar</button>
    </div>
    <div class="modal-hint">
      El código cambia cada 30 segundos. Sin guardar en disco.
    </div>
  </div>
</div>

<!-- Thin progress bar pinned to the top of the viewport. -->
<div id="progress-bar" class="progress-bar"></div>

<!-- Non-blocking toast under the top-bar — shows the current stage. -->
<div id="toast" class="toast">
  <button id="toast-close-btn" class="t-close" aria-label="Cerrar">×</button>
  <div class="t-title">
    <span class="spin"></span>
    <span id="toast-title">Actualizando tu portafolio</span>
  </div>
  <div class="t-stage" id="progress-stage">Conectando con GBM…</div>
</div>
`;

function injectSharedChromeCss() {
  if (document.getElementById("shared-chrome-css")) return;
  const style = document.createElement("style");
  style.id = "shared-chrome-css";
  style.textContent = SHARED_CHROME_CSS;
  document.head.appendChild(style);
}

// The tabs in the top-bar — single source of truth.
const TABS = [
  { tab: "portfolio",    href: "/app/index.html",        label: "📊 Portafolio" },
  { tab: "analysis",     href: "/app/analysis.html",     label: "📈 Análisis" },
  { tab: "orders",       href: "/app/orders.html",       label: "📋 Órdenes" },
  { tab: "dividends",    href: "/app/dividends.html",    label: "💰 Dividendos" },
  { tab: "transactions", href: "/app/transactions.html", label: "📒 Libro Diario" },
  { tab: "glossary",     href: "/app/glossary.html",     label: "📖 Glosario" },
  { tab: "settings",     href: "/app/settings.html",     label: "⚙ Configuración" },
];

// Inject the sticky top-bar at the start of <body>. Each page declares
// which tab is active via <body data-tab="portfolio">; if omitted we
// infer it from the URL. Idempotent.
function injectTopBar() {
  if (document.querySelector(".top-bar")) return;

  const activeTab = document.body.dataset.tab || _tabFromPath();

  const bar = document.createElement("div");
  bar.className = "top-bar";
  bar.innerHTML = `
    <div class="brand">
      <div class="brand-logo">GBM</div>
      <span class="brand-title">GBM Dashboard</span>
    </div>
    <nav></nav>
    <div class="actions">
      <button class="update-btn" id="update-btn" type="button">🔄 Actualizar</button>
    </div>
  `;
  const nav = bar.querySelector("nav");
  for (const t of TABS) {
    const a = document.createElement("a");
    a.href = t.href;
    a.textContent = t.label;
    a.dataset.tab = t.tab;
    if (t.tab === activeTab) a.className = "active";
    nav.appendChild(a);
  }
  bar.querySelector("#update-btn").addEventListener("click", () => triggerUpdate());

  document.body.insertBefore(bar, document.body.firstChild);
}

function _tabFromPath() {
  const p = location.pathname;
  if (p.endsWith("/analysis.html")) return "analysis";
  if (p.endsWith("/orders.html")) return "orders";
  if (p.endsWith("/dividends.html")) return "dividends";
  if (p.endsWith("/transactions.html")) return "transactions";
  if (p.endsWith("/glossary.html")) return "glossary";
  if (p.endsWith("/settings.html")) return "settings";
  return "portfolio";  // index.html or anything else defaults to portfolio
}

// Add only the staleness chip into the page's .subtitle (the page itself
// still owns the surrounding "Última actualización: <timestamp>" text).
// The chip's initial color is set by the page when it calls renderHeader
// after reading last_update.date; AFTER that, refreshStalenessChip below
// keeps it current (60s poll + cross-tab BroadcastChannel signal).
function injectStalenessChip() {
  if (document.getElementById("last-update-age")) return;
  const subtitle = document.querySelector(".subtitle");
  if (!subtitle) return;
  const chip = document.createElement("span");
  chip.id = "last-update-age";
  chip.className = "staleness-chip";
  subtitle.appendChild(document.createTextNode(" "));
  subtitle.appendChild(chip);
}

// Re-fetch /DATA/last_update.date and re-paint the chip. Safe to call
// repeatedly. Used by the 60s poll (so a "5 min ago" tab rolls over to
// "6 min ago" without a reload) and by the cross-tab BroadcastChannel
// listener (so a tab that didn't trigger the update still catches up).
async function refreshStalenessChip() {
  const chip = document.getElementById("last-update-age");
  if (!chip) return;
  try {
    const r = await fetch("/DATA/last_update.date?t=" + Date.now(), { cache: "no-store" });
    if (!r.ok) return;
    const ts = (await r.text()).trim();
    if (!ts) return;
    const stale = stalenessHint(ts);
    if (!stale) return;
    chip.textContent = stale.label;
    chip.className = "staleness-chip show " + stale.severity;
    const hint = stale.severity === "stale"
      ? "Tu snapshot es viejo — dale 🔄 Actualizar."
      : stale.severity === "warn"
      ? "Tu snapshot tiene más de 15 min."
      : "Datos frescos.";
    chip.title = `${formatTimestamp(ts)}\n${hint}`;
  } catch (_) { /* keep prior state */ }
}

// Cross-tab signaling: when Update Now finishes in one tab, broadcast
// so the chip in OTHER tabs refreshes instantly. Falls back gracefully
// to the 60s poll below on browsers without BroadcastChannel.
let _gbmUpdateChannel = null;
try {
  _gbmUpdateChannel = new BroadcastChannel("gbm-dashboard-update");
  _gbmUpdateChannel.onmessage = (e) => {
    if (e.data && e.data.type === "update-complete") {
      refreshStalenessChip();
    }
  };
} catch (_) { /* old browser */ }
function broadcastUpdateComplete() {
  if (_gbmUpdateChannel) {
    try { _gbmUpdateChannel.postMessage({ type: "update-complete", t: Date.now() }); } catch (_) {}
  }
}

function injectSharedChromeHtml() {
  if (document.getElementById("totp-modal")) return;  // already present
  const tpl = document.createElement("template");
  tpl.innerHTML = SHARED_CHROME_HTML.trim();
  document.body.appendChild(tpl.content);
}

// ----------------------------------------------------------------------
// Update + TOTP flow
// ----------------------------------------------------------------------
// Refresh the page-specific data after a successful update. Pages that
// can re-render in place (portfolio, orders, dividends, transactions)
// should expose `window.onUpdateComplete = async () => { ... }`.
async function refreshDashboardData() {
  if (typeof window.onUpdateComplete === "function") {
    try {
      await window.onUpdateComplete();
      return;
    } catch (e) {
      // Page-specific reload failed — fall back to a hard reload so the
      // user at least sees fresh data.
      // eslint-disable-next-line no-console
      console.error("onUpdateComplete failed, doing hard reload:", e);
    }
  }
  location.reload();
}

async function triggerUpdate(totpCode = null, opts = {}) {
  // opts.full: bypass incremental and force a full-window refetch.
  // Used by "Recargar todo desde cero" in the TOTP modal and by the
  // Settings page's force-reload button.
  const fullReload = opts.full === true;
  const btn = document.getElementById("update-btn");
  btn.disabled = true;
  btn.textContent = totpCode ? "🔄 Verificando código..." : "🔄 Conectando...";

  // Defer the heavy overlay: the first call (no TOTP) might immediately
  // come back with mfa_required, in which case we don't want to flash
  // the overlay before opening the TOTP modal. When a TOTP code IS
  // present we already know the fetch will run for minutes, so show
  // the overlay right away.
  let overlayShown = false;
  let pollTimer = null;
  const startOverlay = () => {
    if (overlayShown) return;
    overlayShown = true;
    // If we got here from the TOTP modal (user just typed a code),
    // close that modal as the progress overlay takes over — otherwise
    // both stack on top of each other and look broken.
    if (totpCode) closeModal();
    showProgressOverlay();
    pollTimer = startProgressPolling();
    btn.textContent = "🔄 Actualizando...";
  };
  // 0 ms delay when the user just typed a TOTP — the fetch WILL be slow.
  // 700 ms when this is the first probe — quick responses (mfa_required,
  // bad credentials) get to dismiss before the overlay even appears.
  const overlayDelay = totpCode != null ? 0 : 700;
  const overlayTimer = setTimeout(startOverlay, overlayDelay);
  const stopOverlay = () => {
    if (overlayTimer) clearTimeout(overlayTimer);
    if (pollTimer) {
      stopProgressPolling(pollTimer);
      pollTimer = null;
    }
    if (overlayShown) {
      hideProgressOverlay();
      overlayShown = false;
    }
  };

  let res;
  try {
    const reqBody = {};
    if (totpCode) reqBody.totp_code = totpCode;
    if (fullReload) reqBody.full = true;
    res = await fetch("/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(reqBody),
    });
  } catch (err) {
    stopOverlay();
    btn.disabled = false;
    btn.textContent = "🔄 Actualizar";
    alert("No se pudo conectar al server.\nDetalle: " + err.message);
    return;
  }

  clearTimeout(overlayTimer);
  if (pollTimer) stopProgressPolling(pollTimer);

  let payload = {};
  try { payload = await res.json(); } catch (_) {}

  if (res.ok && payload.status === "ok") {
    closeModal();
    btn.textContent = "🔄 Refrescando vista...";
    await refreshDashboardData();
    broadcastUpdateComplete();   // tell other tabs to refresh their chip
    stopOverlay();
    btn.disabled = false;
    btn.textContent = "🔄 Actualizar";
    return;
  }

  // Handle the documented error statuses from the server.
  stopOverlay();
  btn.disabled = false;
  btn.textContent = "🔄 Actualizar";

  if (payload.status === "mfa_required") {
    openModal();
    return;
  }
  if (payload.status === "mfa_invalid") {
    openModal("Código incorrecto o ya expiró. Genera uno nuevo en tu app.");
    return;
  }
  if (payload.status === "auth_failed") {
    closeModal();
    openConfigModal();
    document.getElementById("config-error").textContent =
      "Las credenciales son incorrectas o GBM las rechazó. Corrige y reintenta.";
    document.getElementById("config-error").classList.remove("hidden");
    return;
  }
  if (payload.status === "config_error") {
    closeModal();
    openConfigModal(true);
    return;
  }
  if (payload.status === "api_error" || payload.status === "timeout") {
    closeModal();
    alert("La API de GBM falló: " + (payload.detail || "sin detalle"));
    return;
  }
  closeModal();
  alert("Update falló (HTTP " + res.status + "): " + (payload.detail || "sin detalle"));
}

// Polls the TOTP input state while the modal is open. Stored on the
// module so closeModal can clear it. The interval is cheap (~200 ms,
// just reads .value and toggles a `disabled` attr) but never wasted
// because closeModal kills it the instant the modal goes away.
let _totpPollTimer = null;

function openModal(errorMsg = null) {
  const modal = document.getElementById("totp-modal");
  const errEl = document.getElementById("totp-error");
  const input = document.getElementById("totp-input");
  if (errorMsg) {
    errEl.textContent = errorMsg;
    errEl.classList.remove("hidden");
  } else {
    errEl.classList.add("hidden");
  }
  input.value = "";
  document.getElementById("totp-submit").disabled = true;
  modal.classList.add("show");
  setTimeout(() => input.focus(), 100);
  // Safety-net poll: even when paste / autofill / IME composition
  // skips the `input` event, the button will catch up within 200 ms.
  if (_totpPollTimer) clearInterval(_totpPollTimer);
  _totpPollTimer = setInterval(revalidateTotpSubmit, 200);
}

function closeModal() {
  const m = document.getElementById("totp-modal");
  if (m) m.classList.remove("show");
  if (_totpPollTimer) {
    clearInterval(_totpPollTimer);
    _totpPollTimer = null;
  }
}

function closeModalIfBackdrop(e, which) {
  const id = which === "config" ? "config-modal" : "totp-modal";
  if (e.target.id === id) {
    if (which === "config") closeConfigModal();
    else closeModal();
  }
}

// Re-evaluates the TOTP submit button state based on the current
// input value. Called from MULTIPLE events (input, paste, change,
// focus, blur) AND from a polling interval while the modal is open
// — Chrome autofill / password-manager fills sometimes don't fire
// the `input` event, leaving the button stuck on `disabled` even
// when there's a valid 6-digit code in the field. The poll is the
// safety net; it stops as soon as the modal closes.
function revalidateTotpSubmit() {
  const inp = document.getElementById("totp-input");
  const btn = document.getElementById("totp-submit");
  if (!inp || !btn) return;
  // Strip non-digits + cap to 6 (in case paste pushed extra chars).
  const cleaned = inp.value.replace(/\D/g, "").slice(0, 6);
  if (inp.value !== cleaned) inp.value = cleaned;
  btn.disabled = cleaned.length !== 6;
}

// Kept as a thin wrapper for the inline `oninput="onTotpInput(event)"`
// attribute on the modal input (CSP-restricted contexts use
// addEventListener instead and call revalidateTotpSubmit directly).
function onTotpInput(_e) {
  revalidateTotpSubmit();
}

// Verbatim port of Trade-Republic-owncloud's submitMfa() flow:
// read state → close modal SYNC → show toast SYNC → await fetch
// → handle response. Self-contained on purpose: delegating to the
// shared triggerUpdate() (which uses setTimeout(startOverlay, 0))
// occasionally left the modal open and the toast invisible
// because the deferred startOverlay wasn't firing reliably. TR's
// pattern has worked unmodified for months; mirror it.
async function submitTotp() {
  const code = document.getElementById("totp-input").value.trim();
  const errEl = document.getElementById("totp-error");
  errEl.classList.add("hidden");
  if (!(code.length === 6 && /^\d+$/.test(code))) {
    errEl.textContent = "El código debe ser de 6 dígitos.";
    errEl.classList.remove("hidden");
    return;
  }
  const fullEl = document.getElementById("totp-full-reload");
  const fullReload = fullEl ? fullEl.checked === true : false;
  const submitBtn = document.getElementById("totp-submit");
  const topBtn = document.getElementById("update-btn");
  submitBtn.disabled = true;
  submitBtn.textContent = "Verificando...";
  topBtn.disabled = true;
  topBtn.textContent = fullReload ? "🔄 Re-descargando todo..." : "🔄 Actualizando...";

  // Close modal + show toast SYNCHRONOUSLY before the fetch — no
  // setTimeout, no race. Identical to TR's submitMfa().
  closeModal();
  showProgressOverlay();
  const pollTimer = startProgressPolling();

  let res;
  try {
    res = await fetch("/update", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ totp_code: code, ...(fullReload ? { full: true } : {}) }),
    });
  } catch (err) {
    stopProgressPolling(pollTimer);
    hideProgressOverlay();
    topBtn.disabled = false;
    topBtn.textContent = "🔄 Actualizar";
    submitBtn.disabled = false;
    submitBtn.textContent = "Actualizar";
    alert("No se pudo conectar al server.\nDetalle: " + err.message);
    return;
  }

  stopProgressPolling(pollTimer);

  let payload = {};
  try { payload = await res.json(); } catch (_) {}

  if (res.ok && payload.status === "ok") {
    topBtn.textContent = "🔄 Refrescando vista...";
    await refreshDashboardData();
    broadcastUpdateComplete();
    hideProgressOverlay();
    topBtn.disabled = false;
    topBtn.textContent = "🔄 Actualizar";
    submitBtn.disabled = false;
    submitBtn.textContent = "Actualizar";
    return;
  }

  hideProgressOverlay();
  topBtn.disabled = false;
  topBtn.textContent = "🔄 Actualizar";
  submitBtn.disabled = false;
  submitBtn.textContent = "Actualizar";

  if (payload.status === "mfa_invalid") {
    openModal("Código incorrecto o ya expiró. Genera uno nuevo en tu app.");
    return;
  }
  if (payload.status === "mfa_required") {
    openModal();
    return;
  }
  if (payload.status === "auth_failed") {
    openConfigModal();
    const cfgErr = document.getElementById("config-error");
    cfgErr.textContent = "Las credenciales son incorrectas o GBM las rechazó. Corrige y reintenta.";
    cfgErr.classList.remove("hidden");
    return;
  }
  if (payload.status === "config_error") {
    openConfigModal(true);
    return;
  }
  if (payload.status === "api_error" || payload.status === "timeout") {
    alert("La API de GBM falló: " + (payload.detail || "sin detalle"));
    return;
  }
  alert("Update falló (HTTP " + res.status + "): " + (payload.detail || "sin detalle"));
}

// ----------------------------------------------------------------------
// Config modal (email + password)
// ----------------------------------------------------------------------
async function loadConfigStatus() {
  try {
    const res = await fetch("/config");
    return await res.json();
  } catch (_) {
    return { configured: false, email: null };
  }
}

async function maybeShowConfigOnFirstLoad() {
  const status = await loadConfigStatus();
  if (!status.configured) {
    openConfigModal(/* firstTime = */ true);
  }
}

function openConfigModal(firstTime = false) {
  const modal = document.getElementById("config-modal");
  const errEl = document.getElementById("config-error");
  errEl.classList.add("hidden");
  loadConfigStatus().then(s => {
    const emailEl = document.getElementById("config-email");
    const pwEl = document.getElementById("config-password");
    if (s.email && !firstTime) emailEl.value = s.email;
    pwEl.value = "";
    document.getElementById("config-submit").disabled = true;
    modal.classList.add("show");
    setTimeout(() => (s.email ? pwEl : emailEl).focus(), 100);
  });
}

function closeConfigModal() {
  const m = document.getElementById("config-modal");
  if (m) m.classList.remove("show");
}

function onConfigInput() {
  const email = document.getElementById("config-email").value.trim();
  const pw = document.getElementById("config-password").value;
  const ok = email.includes("@") && pw.length >= 4;
  document.getElementById("config-submit").disabled = !ok;
}

async function submitConfig() {
  const email = document.getElementById("config-email").value.trim();
  const password = document.getElementById("config-password").value;
  const btn = document.getElementById("config-submit");
  const errEl = document.getElementById("config-error");
  btn.disabled = true;
  btn.textContent = "Guardando...";

  try {
    const res = await fetch("/config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ email, password }),
    });
    const payload = await res.json();
    if (res.ok && payload.status === "ok") {
      btn.textContent = "Guardar";
      closeConfigModal();
      // If the page exposed an account-change callback (portfolio page
      // wipes its in-memory state), let it run before we kick off the
      // new update. Other pages just rely on the post-update reload.
      if (payload.account_changed && typeof window.onAccountChanged === "function") {
        try { await window.onAccountChanged(); } catch (_) {}
      }
      // Kick off an update — server will report mfa_required and the
      // TOTP modal will appear automatically.
      triggerUpdate();
      return;
    }
    errEl.textContent = payload.detail || "Error guardando credenciales.";
    errEl.classList.remove("hidden");
  } catch (err) {
    errEl.textContent = "No se pudo conectar al servidor.";
    errEl.classList.remove("hidden");
  }
  btn.textContent = "Guardar";
  onConfigInput();
}

// ----------------------------------------------------------------------
// Progress overlay (shown during /update fetches).
// Spanish, friendly, no technical noise. The stage message changes
// based on elapsed time so the user knows things are still happening.
// ----------------------------------------------------------------------
const PROGRESS_STAGES = [
  { until: 3,    text: "Conectando con GBM…" },
  { until: 12,   text: "Descargando tu portafolio…" },
  { until: 45,   text: "Descargando posiciones…" },
  { until: 120,  text: "Descargando historial de operaciones…" },
  { until: 180,  text: "Ya casi terminamos…" },
  { until: Infinity, text: "Sigue trabajando, espera un poco más…" },
];

let _progressStartedAt = null;

function showProgressOverlay() {
  // Surface the thin top progress-bar + the toast under the top-bar.
  // Both are non-blocking — page stays interactive while the fetch runs.
  const bar = document.getElementById("progress-bar");
  const toast = document.getElementById("toast");
  const stage = document.getElementById("progress-stage");
  const title = document.getElementById("toast-title");
  if (bar) bar.classList.add("active", "indet");
  if (toast) {
    toast.classList.remove("ok", "err");
    toast.classList.add("active");
  }
  if (title) title.textContent = "Actualizando tu portafolio";
  if (stage) stage.textContent = PROGRESS_STAGES[0].text;
  _progressStartedAt = Date.now();
}

function hideProgressOverlay() {
  const bar = document.getElementById("progress-bar");
  const toast = document.getElementById("toast");
  if (bar) bar.classList.remove("active", "indet");
  if (toast) toast.classList.remove("active");
  _progressStartedAt = null;
}

function startProgressPolling() {
  const updateStage = () => {
    if (_progressStartedAt == null) return;
    const elapsed = (Date.now() - _progressStartedAt) / 1000;
    const stage = PROGRESS_STAGES.find(s => elapsed < s.until)
                  || PROGRESS_STAGES[PROGRESS_STAGES.length - 1];
    const el = document.getElementById("progress-stage");
    if (el.textContent !== stage.text) el.textContent = stage.text;
  };
  updateStage();
  return setInterval(updateStage, 1000);
}

function stopProgressPolling(timer) {
  if (timer != null) clearInterval(timer);
}

// ----------------------------------------------------------------------
// Bootstrap
// ----------------------------------------------------------------------
window.addEventListener("DOMContentLoaded", () => {
  // Stamp the version badge in the footer. We show the dashboard version
  // synchronously (it's a build-time constant) and then async-fetch
  // /settings to append the gbm-mx-api version once it's known. If the
  // fetch fails, the footer just keeps the dashboard-only string.
  for (const el of document.querySelectorAll("[data-version]")) {
    el.textContent = `gbm-dashboard v${VERSION}`;
  }
  fetch("/settings", { cache: "no-store" })
    .then(r => (r.ok ? r.json() : null))
    .then(s => {
      if (!s || !s.gbm_mx_api_version) return;
      for (const el of document.querySelectorAll("[data-version]")) {
        el.textContent = `gbm-dashboard v${VERSION} · gbm-mx-api v${s.gbm_mx_api_version}`;
      }
    })
    .catch(() => { /* keep dashboard-only stamp on failure */ });

  // Inject the shared chrome: CSS, top-bar (brand + tabs + Actualizar),
  // staleness chip in the subtitle, and modals + progress overlay.
  injectSharedChromeCss();
  injectTopBar();
  injectStalenessChip();
  // Keep the chip current — re-fetch every minute so "5 min ago"
  // rolls over to "6 min ago" without a reload, and so a tab that
  // didn't trigger the update still catches up to one that did.
  setInterval(refreshStalenessChip, 60_000);
  injectSharedChromeHtml();

  // Toast close button (dismisses the non-blocking status banner manually).
  const toastClose = document.getElementById("toast-close-btn");
  if (toastClose) toastClose.addEventListener("click", () => {
    document.getElementById("toast").classList.remove("active");
  });

  // Escape closes any open modal.
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    closeModal();
    closeConfigModal();
  });

  // First-run check: if there are no credentials, open the config modal.
  maybeShowConfigOnFirstLoad();
});
