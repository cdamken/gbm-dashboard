// Shared helpers for every dashboard page.
// Loaded as <script src="/app/_shared.js"></script> before each page's
// inline <script>. Exposes everything as globals — kept simple on purpose
// (no modules, no bundler, no build step).

const VERSION = "0.8.3";

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

// Stamp the version badge in the footer. Pages just need a
// <span data-version></span> somewhere; this picks it up on load.
window.addEventListener("DOMContentLoaded", () => {
  for (const el of document.querySelectorAll("[data-version]")) {
    el.textContent = `gbm-dashboard v${VERSION}`;
  }
});
