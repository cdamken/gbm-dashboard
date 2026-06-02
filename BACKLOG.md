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

- [x] **Range pills (1M / 3M / 6M / 1Y / All)** — implementado 2026-06-02
  en el chart de "Capital invertido" en Análisis. Filtra los puntos del
  array antes de renderear. El bar chart de dividendos siempre muestra
  12 meses (no necesita pills). Las pills 1W están omitidas — granularidad
  diaria con < 7 puntos no es informativa.

- [x] **Position detail modal con links externos** — implementado
  2026-06-02. Click en un ticker abre un panel lateral con: cantidad,
  precio promedio, último precio, costo invertido, valor mercado, P&L.
  Links externos market-aware: BMV → Google Finance:BMV + Yahoo .MX +
  página BMV; SIC/Trading USA → Google Finance + Yahoo + Stock Analysis;
  fondos → solo Google search. Tickers con asterisco (SIC) se limpian
  para los lookups. Ver `index.html::openPositionModal`.

- [x] **Página Glossary** — implementado 2026-06-02. `/app/glossary.html`
  con 5 secciones (Mercados, Cuentas, Fiscal SAT, Métricas, Categorías
  del Libro Diario) ~25 términos. Búsqueda en vivo, secciones colapsan
  cuando no tienen coincidencias. Tab nueva en top-bar entre Libro
  Diario y Configuración.

- [x] **Switch account UX explícito** — implementado 2026-06-02. Botón
  "🔄 Cambiar a otra cuenta…" en Configuración → sección Cuenta. Pide
  confirmación, vacía email + password, focuses email. Al guardar con
  otro email el flow existente (account_changed en /config POST) hace
  el wipe automático.

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

- [x] **Investigar si GBM expone historial > 365 días** — confirmado
  2026-06-02 leyendo `gbm-mx-api::api/transactions.py`. La API misma
  NO impone límite — el 365 default es nuestro. El endpoint
  `https://api.appgbm.com/v2/trading/contracts/{id}/transactions`
  acepta cualquier `start_date`/`end_date`. El user puede subir
  `Libro Diario (días)` en Settings → Rangos de datos a 1095/1825+
  y XIRR/línea de patrimonio empezarán a dar números útiles.
  El server-side de GBM puede tener su propio límite que no conocemos
  hasta probar; el tip está documentado en settings.html.

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

- [x] **Benchmark replay** — implementado 2026-06-02. Nuevo endpoint
  server-side `GET /benchmark/{symbol}` proxea Yahoo Finance v8 chart
  (cache 24h en `DATA/benchmark_cache/`). En Análisis, el chart de
  "Capital invertido" ahora superpone NAFTRACISHRS.MX (línea ámbar
  punteada) y SPY (verde punteada). Algoritmo: para cada mes con net
  flow ≠ 0, allocate flow / close → cumulative units → value = units
  × close. Mismo patrón que TR's replay_against_benchmark. Símbolos
  validados por regex en el server (defensa contra path traversal).

- [ ] **Lifetime P&L / Net capital in metric** — métrica que separa
  capital comprometido (depósitos − retiros) del rendimiento puro.
  Tilt fiscal: útil para reportar al SAT.

- [ ] **Tax Refund como tipo de transacción separado** — el SAT a veces
  devuelve ISR retenido en exceso. Hoy ese movimiento se mezcla con
  "depósito". Categorizarlo aparte para que las métricas no
  contaminen el "capital comprometido".

## 3. Datos / export

- [x] **Export CSV para SAT** — implementado 2026-06-02. Endpoint
  `GET /export/transactions.csv` retorna CSV con 13 columnas en español
  (fecha/hora/tipo/categoria/descripcion/ticker/cuenta/monto/monto_neto/
  comision/iva/isr_retenido_o_tax/transaccion_id). Botón "📥 Exportar
  CSV para SAT" en Settings → Rangos de datos. Sourced de
  `transactions.json` (sin API extra). Para algo más fancy (OFX, XML
  SAT) queda como ampliación.

- [ ] **Incremental sync** — diferido. Análisis 2026-06-02: con los
  tamaños actuales del dataset (~23 dividends, ~200 transactions,
  ~100 orders en 365d) un Update completo tarda ~30s. Lo que tarda
  es positions (snapshot, no se puede incrementar) y el v3 dashboard
  endpoint (5-30s server-side). Incremental sync ahorraría 5-10s
  máximo. Costo de implementación alto (tracking de last_fetched +
  merge dedupado) vs beneficio bajo a esta escala. Reconsiderar
  cuando el dataset crezca (ej. >1000 transactions) o si subes
  `GBM_TRANSACTIONS_DAYS` a 1095+ y el fetch se vuelve lento.

## 4. Seguridad / robustez

- [x] **CSRF check en POST endpoints** — ya en server.py via Origin header.
- [x] **Auto-refresh de sesión Cognito** — gbm-mx-api 0.3.0.
- [x] **Endpoint /revoke real** — implementado 2026-06-02 con
  `gbm-mx-api 0.3.1::auth.global_signout()`. POST /reset ahora:
  (1) refresca el access_token si expiró, (2) llama a Cognito
  GlobalSignOut → refresh_token inválido server-side, (3) wipea local.
  Best-effort: si Cognito está unreachable, el wipe local sigue
  funcionando. La respuesta incluye `signed_out_globally: bool` y
  `signout_detail` para que el UI distinga "revocación completa" de
  "solo borrado local".

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
