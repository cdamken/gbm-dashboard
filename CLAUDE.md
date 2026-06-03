# CLAUDE.md — gbm-dashboard

> Context for AI assistants. Humans: see [README.md](README.md).

## What this is

Local single-user dashboard for **GBM+** (Grupo Bursátil Mexicano,
casa de bolsa mexicana). Runs a Python HTTP server on `localhost:8086`
and renders a dark-themed UI for portfolio + análisis. Uses
[`gbm-mx-api`](https://github.com/cdamken/gbm-mx-api) as a library.

## Position in the trio

```
   gbm-mx-api (library)  ──┐
                           ├──► gbm-dashboard   (this repo — upstream)
                           │      │
                           │      │  port: copy verbatim + minimal ownCloud patches
                           │      ▼
                           └──► gbm-owncloud   (multi-user ownCloud app)
```

**This repo is upstream for the ownCloud port.** Any UI/UX or
data-shaping change should land here first. The ownCloud port
(`gbm-owncloud`) re-copies the touched files verbatim and applies only
forced multi-user patches (per-user paths, CSP, ICrypto-encrypted
credentials).

The orchestrator lives in `~/damkencloud/Claude/GBM-Master/` — its
`docs/WORKFLOW.md` is the canonical cross-repo flow.

## Workflow rule

When fixing a bug or adding a feature:

1. Land it here first.
2. Verify locally (run `./dashboard.sh`, do ⟳ Actualizar).
3. Port to `gbm-owncloud` (mostly copy-paste of the touched files; UI
   verbatim, JS paths translated to ownCloud `data-route-*`, Python
   data dir is per-user).
4. Bump `appinfo/info.xml` version and deploy via `scripts/deploy.sh`.

If a change is protocol-level (endpoints, auth, account types), land
it in `gbm-mx-api` first.

## Architecture

```
┌────────────────────────────────────────────────────┐
│  Browser → http://localhost:8086/app/index.html    │
└──────────────────┬─────────────────────────────────┘
                   │ HTTP
┌──────────────────▼─────────────────────────────────┐
│  app/server.py  (Python stdlib http.server)        │
│   • /app/*                  → static files          │
│   • /DATA/*.json           → JSON output           │
│   • /config (GET/POST)     → credentials (email/pw)│
│   • /settings (GET/POST)   → days-back ranges      │
│   • /update (POST)         → invokes fetch_data.py │
│   • /reset (POST)          → Cognito GlobalSignOut │
│   • /export/transactions.csv → SAT-friendly CSV    │
│   • /benchmark/{symbol}    → Yahoo Finance proxy   │
│   • /progress              → update progress poll  │
└──────────────────┬─────────────────────────────────┘
                   │ subprocess
┌──────────────────▼─────────────────────────────────┐
│  app/fetch_data.py                                 │
│   • Reads email+password from ~/.gbm-mx/...        │
│   • Persists Cognito session ~/.gbm-mx/session.json│
│   • Hits contracts → accounts → positions/orders   │
│     /dividends /transactions per account           │
│   • Writes DATA/*.json + DATA/transactions.csv     │
└────────────────────────────────────────────────────┘
```

## Páginas (7)

| Página | Archivo | Función |
|---|---|---|
| Portafolio | `app/index.html` | Posiciones + KPIs por cuenta |
| Movimientos | `app/orders.html` | Órdenes llenas (filled) |
| Histórico | `app/orders_all.html` | Cualquier estado (legacy) |
| Dividendos | `app/dividends.html` | Distribuciones de efectivo |
| Libro Diario | `app/transactions.html` | Todas las transacciones (cash) |
| Análisis | `app/analysis.html` | Charts + XIRR + benchmarks |
| Glosario | `app/glossary.html` | Definiciones |
| Configuración | `app/settings.html` | Credenciales + rangos + sesión |

Shared helpers: `app/_shared.js` (formatters, staleness chip, top-bar
auto-refresh, theme).

## Key concepts (read before touching analítica)

### Cuentas y backend

GBM+ usa **AWS Cognito** (`us-east-1_BKu7qAohu`) detrás de
`auth.gbm.com`. Tres backends activos:
- `auth.gbm.com/api/v1/...` — login + MFA TOTP.
- `api.gbm.com/v1,v2/...` — REST moderno (contracts, accounts).
- `homebroker-api.gbm.com/GBMP/api/...` — legacy (PortfolioSummary,
  GetBlotterOrders, GetCashHistoricalMovements).

Headers `device-latitude` / `device-longitude` son **obligatorios** en
todas las requests (anti-fraude).

### Mapping cuenta → operaciones

| Tipo de cuenta | Posiciones | Órdenes (filled) | Dividendos | Transactions |
|---|---|---|---|---|
| Personal (Trading MX) | ✓ | ✓ `GetBlotterOrders` | ✓ | ✓ `GetCashHistoricalMovements` |
| Asesor | ✓ | ❌ (Personal-only) | ✓ | ✓ |
| Smart Cash | ✓ | ❌ | ✓ | ✓ |
| Trading USA | ✓ | ❌ (gap) | — | — |

`transac_type` viene de la API pero **se ignora** excepto para
`dividend`. La categorización real se hace en el dashboard usando el
signo del monto y el `description`.

### CSV export para SAT

`/export/transactions.csv` produce el CSV en español, columnas:
Fecha, Cuenta, Tipo, Descripción, Monto MXN, Símbolo. Diseñado para
pasar al contador o importar a Excel.

### Auto-refresh de sesión (v0.3.1)

`gbm-mx-api 0.3.1+` usa el `refresh_token` de Cognito automáticamente
para evitar re-MFA cada hora. Antes de v0.3.1 cada `/update` después
de 1h pedía TOTP. `/reset` hace `GlobalSignOut` (revoca todos los
dispositivos).

## File layout

```
app/
├── server.py                Python HTTP server + /update flow + MFA modal
├── fetch_data.py            gbm-mx-api → DATA/*.json + transactions.csv
├── _shared.js               Shared formatters + staleness chip + nav top-bar
├── index.html               Portfolio (KPIs + positions table)
├── orders.html              Órdenes llenas
├── orders_all.html          Histórico (legacy)
├── dividends.html           Dividendos
├── transactions.html        Libro Diario
├── analysis.html            Análisis (charts + XIRR + benchmarks)
├── settings.html            Configuración
└── glossary.html            Glosario
DATA/                         Generated; gitignored
├── accounts.json
├── positions.json
├── orders.json
├── dividends.json
├── transactions.json + transactions.csv
└── last_update.date
dashboard.sh                  Launcher (start/stop/restart/update/status)
```

## Idioma

- Conversaciones con Carlos: **español**.
- Código, identificadores, docstrings, commits: **inglés**.
- Strings de UI: **español** (matchea la audiencia de GBM México).

## Recently resolved

- **2026-05-26**: Transactions API + Libro Diario completo (Personal,
  Asesor, Smart Cash). Trading USA pendiente.
- **2026-05-28**: Auto-refresh de sesión vía `refresh_token`
  (gbm-mx-api 0.3.0).
- **2026-05-28**: `/reset` ahora hace `GlobalSignOut` (gbm-mx-api 0.3.1).
- **2026-06-02**: Staleness chip con color + auto-refresh; nav
  reordenado; settings page con sidebar + switch account UX.

## Disclaimer

App **no oficial**. No afiliada con Grupo Bursátil Mexicano. Datos vía
`gbm-mx-api` (reverse-engineered). Los endpoints pueden cambiar sin aviso.
