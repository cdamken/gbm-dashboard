# Backlog — gbm-dashboard

Cosas pendientes / ideas para futuras sesiones. Edita libremente.
Items con `[ ]` están pendientes; `[x]` cuando se completen.

Casi todo viene de la comparación con `Trade-Republic-Dashboard` (sesión
2026-06-02). El orden dentro de cada sección es por impacto estimado,
no por orden de implementación — agarra el que tenga sentido en su momento.

---

## 1. UX / visual

- [ ] **Brand-adjacent dark theme (opción A)** — ajustar acentos a colores
  más "oficiales" de GBM (gbm.com) sin abandonar el dark theme ni la
  densidad. Cambios:
  - `--blue: #60a5fa` → algo más profundo tipo `#1e88e5` (más cercano al azul GBM)
  - Agregar `--accent-teal: #00b8a9` para CTAs secundarios (turquesa de gbm.com)
  - Refinar el gradient del `.brand-logo` para sentirse más navy corporativo
  - Mantener verde/rojo/ámbar funcionales para P&L y market pills
  - NO es rebrand completo, NO light theme, NO whitespace-heavy. ~1h.

- [ ] **Range pills (1W / 1M / 3M / 6M / 1Y / All)** en los gráficos
  cuando se agreguen. Patrón directo de TR. Trivial una vez que existan charts.

- [x] **Position detail modal con links externos** — implementado
  2026-06-02. Click en un ticker abre un panel lateral con: cantidad,
  precio promedio, último precio, costo invertido, valor mercado, P&L.
  Links externos market-aware: BMV → Google Finance:BMV + Yahoo .MX +
  página BMV; SIC/Trading USA → Google Finance + Yahoo + Stock Analysis;
  fondos → solo Google search. Tickers con asterisco (SIC) se limpian
  para los lookups. Ver `index.html::openPositionModal`.

- [ ] **Página Glossary** (en español) — términos de inversión MX:
  ISR, IVA, SIC, BMV, FIBRA, repo, etc. Tab nueva en el top-bar.

- [ ] **Switch account UX explícito** — actualmente cambiar email en
  Configuración hace el switch implícito. Botón "Cambiar de cuenta"
  separado que abra el flow con context claro.

## 2. Gráficos & analítica (lo más impactante — el dashboard hoy es 100% tablas)

- [x] **Ring chart de allocation** — implementado 2026-06-02. Chart.js
  via CDN, doughnut con 6 buckets (BMV / SIC / Extranjero / F. Común /
  F. Deuda / Efectivo) coloreados según las market pills existentes.
  Ver `index.html::renderAllocationChart`.

- [x] **Línea de patrimonio en el tiempo** — implementado 2026-06-02
  en `analysis.html`. Cost-basis trajectory desde transacciones
  (cumulative buys − sells). Ver `renderNetWorthChart`. Misma
  limitación de XIRR: solo 365 días visibles, así que la línea
  representa el delta dentro de esa ventana, no la base absoluta.

- [x] **Barras mensuales de dividendos** — implementado 2026-06-02
  en `analysis.html`. Stacked bars (verde neto + rojo ISR) últimos
  12 meses. Badge total. Ver `renderDividendsChart`.

- [x] **XIRR (money-weighted IRR)** — implementado 2026-06-02. Newton-
  Raphson + bisection fallback portado de TR. KPI card en el header.
  **Limitación documentada**: GBM solo expone los últimos 365 días de
  transacciones. Si la mayor parte de tu capital se depositó antes,
  XIRR no puede reconciliar y muestra "—". Mejorará cuando haya 12+
  meses de historial visible (subiendo `GBM_TRANSACTIONS_DAYS` si la
  API lo permite, o esperando que pasen los meses). Ver
  `index.html::xirr` + `buildXirrCashflows`.

- [ ] **Investigar si GBM expone historial > 365 días de transacciones**
  — si sí, subir el default de `GBM_TRANSACTIONS_DAYS` haría que XIRR
  empiece a dar números útiles. Probar con valores 730, 1095, 1825 días
  en `app/.env` y ver qué devuelve la API.

- [x] **Yield on cost / dividend forecast 12m** — implementado 2026-06-02
  en `analysis.html` como stat row arriba del bar chart de dividendos.
  Muestra: recibido (12m), ISR retenido (12m), proyección próximos
  12m (escalando observado a 365d). Requiere ≥90 días de historial
  para mostrar la proyección — bajo eso surfacea "necesita más
  historial" en lugar de un número ruidoso. Misma lógica que TR
  `forward_dividend_income`.

- [x] **Concentration warnings** — implementado 2026-06-02. Banner
  ámbar (caution) si top > 30% o top-5 > 70%. Banner rojo (severe)
  si top > 50% o top-5 > 85%. Agrega cross-account, excluye efectivo.
  Ver `index.html::computeConcentration` + `renderConcentrationWarning`.

- [ ] **Benchmark replay** — "qué hubiera pasado si invertía en NAFTRAC
  en lugar de stock picking" (para BMV) y "vs S&P 500/SPY" (para
  Trading USA). Equivalente al overlay MSCI World de TR.

- [ ] **Lifetime P&L / Net capital in metric** — métrica que separa
  capital comprometido (depósitos − retiros) del rendimiento puro.
  Tilt fiscal: útil para reportar al SAT.

- [ ] **Tax Refund como tipo de transacción separado** — el SAT a veces
  devuelve ISR retenido en exceso. Hoy ese movimiento se mezcla con
  "depósito". Categorizarlo aparte para que las métricas no
  contaminen el "capital comprometido".

## 3. Datos / export

- [ ] **Export CSV para SAT** — endpoint `GET /export/transactions.csv`
  con formato compatible (fecha, tipo, monto, ISR retenido, descripción).
  Útil para pasarle data al contador. Si va más ambicioso, OFX o
  XML estándar SAT.

- [ ] **Incremental sync** — hoy cada ⟳ Actualizar baja todo. TR hace
  delta sync (~2-15s vs minutos). Trabajo mayor pero impacto alto si
  Carlos actualiza varias veces al día.

## 4. Seguridad / robustez

- [x] **CSRF check en POST endpoints** — ya en server.py via Origin header.
- [x] **Auto-refresh de sesión Cognito** — gbm-mx-api 0.3.0.
- [ ] **Endpoint /revoke real** — `/reset` wipea local pero NO invalida
  el refresh_token en Cognito server-side. "Logout verdadero" requiere
  llamar a `GlobalSignOut` de Cognito. Para cuando quieras forzar un
  reset real (ej. equipo robado).

## 5. Quality of life menor

- [x] **Tooltip en staleness chip** — implementado 2026-06-02. El
  hover ahora muestra el timestamp exacto en zona CDMX + un hint
  breve sobre la antigüedad. Aplica al chip del index y del análisis.

- [x] **Versión visible en footer** — implementado 2026-06-02.
  Footer ahora muestra `gbm-dashboard vX.Y.Z · gbm-mx-api vA.B.C`.
  Ver `_shared.js` bootstrap fetch a `/settings`.

---

*Última actualización: 2026-06-02. Ver también `BACKLOG.md` en
`Trade-Republic-Dashboard/` para el sibling repo.*
