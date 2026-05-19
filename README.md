# 📊 GBM Dashboard

Dashboard local, privado y rápido para visualizar tu portafolio de **GBM+**
(la casa de bolsa mexicana) con los datos en tiempo real que devuelve la
API interna. Todo corre en `localhost` — nada de los datos sale de tu máquina.

Se alimenta de [`gbm-mx-api`](https://github.com/cdamken/gbm-mx-api) (la
librería Python no oficial que descubrió los endpoints de GBM).

---

## 🎯 Por qué existe

La app web de GBM+ tiene varios problemas para usuarios con muchas posiciones
o que quieren analizar más allá del periodo "hoy":

1. **Es lenta** — cargar la página y refrescar.
2. **No hay búsqueda decente** en posiciones.
3. **Vista por cuenta limitada** — si tienes Trading MX + Trading USA + Asesor + Smart Cash, ver "todo junto" no es trivial.
4. **No exporta histórico** sin pasar por el correo o el PDF.

Este dashboard consume `gbm-mx-api` y renderiza tus datos en HTML local con
buscador, filtros, ordenamiento y resumen multi-cuenta.

---

## 🚀 Quick Start

### Requisitos

- Python 3.10+
- `gbm-mx-api` instalado (lo hace el script en el primer arranque).
- Cuenta de GBM+ con 2FA TOTP activado.

### Primer uso

```bash
cd ~/damkencloud/Claude/gbm-dashboard

# Configurar credenciales (NUNCA al repo)
cp app/.env.example app/.env
$EDITOR app/.env                                 # GBM_EMAIL y GBM_PASSWORD

# Smart update — descarga datos, procesa, arranca server, abre browser
./dashboard.sh
```

La primera vez te pedirá el **código TOTP** de tu app autenticadora.
La sesión se guarda en `~/.gbm-mx/session.json` y dura ~1h — los siguientes
runs no piden TOTP de nuevo hasta que expire.

### Comandos

```
./dashboard.sh              Smart update + arranca server + abre browser  (default)
./dashboard.sh update       Igual que ↑ (alias explícito)
./dashboard.sh start        Solo arranca el server (no toca datos)
./dashboard.sh stop         Detiene el server
./dashboard.sh restart      stop + start
./dashboard.sh status       Inventario, fechas, estado del server
```

---

## 🧠 Cómo funciona

```
┌──────────────────────────────────────────────────────────────────────┐
│  1. gbm-mx-api (Python lib, conecta a GBM con 2FA TOTP)              │
│     GbmClient.contracts.get_main()                                   │
│     GbmClient.accounts.list(contract_id)        → 4 cuentas con P&L  │
│     GbmClient.positions.summary(legacy_id)      → composición        │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  2. fetch_data.py                                                    │
│     Llama la lib, escribe DATA/{accounts,positions}.json             │
│     Calcula totales y guarda en DATA/summary.json                    │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  3. server.py (Python http.server local, puerto 8086)                │
│     Sirve project root → /app/*.html, /DATA/*.json                   │
│     POST /update → ejecuta dashboard.sh update                       │
└──────────────────────────────────────┬───────────────────────────────┘
                                       │
                                       ▼
┌──────────────────────────────────────────────────────────────────────┐
│  4. Browser                                                          │
│  http://localhost:8086/app/index.html                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 📂 Estructura

```
gbm-dashboard/
├── README.md                ← este archivo
├── .gitignore               ← excluye DATA/ y .env del control de versiones
├── dashboard.sh             ← UN solo script (update/start/stop/restart/status)
│
├── app/                     ← código del proyecto (versionable)
│   ├── server.py                 HTTP server local (puerto 8086)
│   ├── fetch_data.py             Usa gbm-mx-api → DATA/*.json
│   ├── .env.example              Plantilla de credenciales
│   ├── .env                      Tus credenciales (gitignored)
│   ├── .venv/                    Virtualenv local (gitignored)
│   └── index.html                Dashboard principal
│
└── DATA/                    ← data descargada (no versionable)
    ├── accounts.json             Output de GbmClient.accounts.list()
    ├── positions.json            Output de GbmClient.positions.summary()
    ├── summary.json              Totales calculados (valor total, P&L sumado)
    ├── last_update.date          Fecha + hora del último update exitoso
    └── last_update.log           Log del último run
```

---

## 📊 Qué muestra el dashboard

### `index.html`

| Sección | Contenido |
|---|---|
| **Cards top** | Valor total, P&L acumulado, # posiciones, última actualización |
| **Resumen por cuenta** | Las 4 estrategias (Trading MX, Trading USA, Asesor, Smart Cash) con su valor y P&L |
| **Top ganadores / perdedores** | Posiciones con mejor / peor P&L en % |
| **Tabla completa de posiciones** | Buscador, filtro por mercado (BMV / SIC / Fondo), sort por cualquier columna |

---

## 🔐 Seguridad

- **Todo es local.** Server corre en `localhost:8086` — no hay tráfico de
  salida excepto cuando `gbm-mx-api` llama a GBM.
- **Credenciales** en `app/.env` (gitignored). Nunca al repo.
- **Token de sesión** en `~/.gbm-mx/session.json` con permisos `0600`.
- **`.gitignore` excluye `DATA/`** — si llegas a hacer git init aquí, no se
  filtran datos sensibles.
- El código TOTP solo se pide en stdin, nunca persistido.

---

## 🐛 Troubleshooting

| Síntoma | Solución |
|---|---|
| `command not found: gbm-mx` | Run `dashboard.sh` — instala la lib automáticamente. |
| `Sesión expirada` | `./dashboard.sh update` te pide TOTP otra vez. |
| Dashboard muestra "Cannot connect" / 404 | Server no está. `./dashboard.sh start`. |
| Puerto 8086 en uso | `./dashboard.sh restart`. |
| Datos viejos en pantalla | `./dashboard.sh update` y refresca el browser. |

---

## 📐 Notas

- **Puerto 8086** está hardcodeado (8085 es de Trade-Republic-Dashboard).
- **Proyecto separado:** la librería `gbm-mx-api` vive aparte. Este dashboard
  es solo el frontend.
- **No tiene snapshot histórico todavía.** GBM no expone "valor del portafolio
  en fecha X" — para tener una serie temporal de net worth, habría que
  acumular snapshots día a día (planeado en una v0.2 del dashboard).

---

## 🗂️ Proyectos relacionados

- **[`gbm-mx-api`](https://github.com/cdamken/gbm-mx-api)** — la librería que
  consume este dashboard.
- **`Trade-Republic-Dashboard`** — proyecto hermano para Trade Republic con la
  misma arquitectura (esta es la versión GBM).

---

*v0.1 — primer entregable: portfolio + posiciones.*
