# ExpenseFlow: Handoff

For whoever deploys or operates ExpenseFlow next. For setup and the full endpoint reference, see [`README.md`](../README.md). For design decisions, see [`ARCHITECTURE.md`](ARCHITECTURE.md) and [`adr/`](adr/).

**Status: proof of concept, not production-ready.** Read [Known limitations and risks](#known-limitations-and-risks) before exposing it to real users.

---

## What it does

ExpenseFlow is an HTTP API for one journey: **submit an expense in any currency → converted to INR → approved or rejected.** It also has:
- spending insights, written by Claude or computed locally
- masking of emails, phone numbers and card numbers in every response
- a Streamlit web UI that talks to the API over HTTP

---

## How it works

```
 Streamlit UI (ui/app.py) ──HTTP + X-API-Key──▶  FastAPI app (app/)  ──▶  SQLite file (expenseflow.db)
                                                      │
                                                      ├──▶ Frankfurter FX API   (on submit, get and approve of unconverted expenses)
                                                      └──▶ Anthropic API        (on GET /expenses/insights, optional)
```

- **One process,** served by Uvicorn. All endpoints are synchronous (`def`), so FastAPI runs them in its thread pool.
- **Storage** is one SQLite table, `expenses`. Integrity rules are enforced by `CHECK` constraints in the database, not just in code: positive amounts, a valid status, and no `approved` row without an INR amount.
- **Money** is stored as integer minor units, and conversion uses `Decimal` with half-up rounding applied once. See [ADR 0001](adr/0001-money-as-integer-minor-units.md).
- **The submit flow:**
  1. The row is saved first.
  2. Then conversion is attempted: INR skips the FX call, and other currencies call `FX_API_URL`.
  3. On any FX failure (timeout, HTTP error, bad JSON, or a missing, zero or huge rate), the row stays saved with `amount_base_minor = NULL`, the error goes in `fx_last_error`, and the API returns **202**. The conversion is retried on the next `GET /expenses/{id}` or approve.
- **Decisions** use a conditional `UPDATE … WHERE status = 'pending'`, so concurrent approvers produce one change. Repeating the same decision is a 200 no-op; changing a decision is a 409.
- **Insights:**
  - With `INSIGHTS_PROVIDER=auto`, the API asks Claude (`claude-sonnet-4-6`) first. On any failure it falls back to insights computed in Python. The response's `source` field says which one you got (`ai` or `rules`).
  - Data sent to Claude is PII-masked, capped at the 200 newest expenses, and wrapped in an `<expense_data>` block that the model is told is untrusted data.
- **Masking** is applied to `description` and `category` on the way out (`ExpenseOut`). The database keeps the original text.
- **Auth** is one shared key. `POST /expenses`, approve, reject and `GET /expenses/insights` need `X-API-Key` equal to `API_KEY`. If `API_KEY` is unset, those requests are refused (fail closed). Other reads are open.

---

## What a deployment engineer needs to know

### Runtime and install
- **Python 3.14.** Dependencies are pinned in `requirements.txt`.
- **Windows with Smart App Control** blocks compiled extensions. Install with `$env:DISABLE_SQLALCHEMY_CEXT='1'`, as described in the README, so SQLAlchemy builds as pure Python. pandas (pulled in by Streamlit) is also blocked there; the UI is written to work without it. On Linux, or on Windows without Smart App Control, none of this applies, but the same install command still works.
- **Start command:** `python -m uvicorn app.main:app --host <addr> --port <port>`.
  - Don't use `--reload` in production; it's for development.
  - The default bind is `127.0.0.1:8000`.
- **Run it from the project root.** The default `DATABASE_URL`, `sqlite:///expenseflow.db`, is a *relative* path, so the database file is created in the working directory. Set an absolute path in production.
- **The UI** is a separate process: `streamlit run ui/app.py` (default port 8501). It needs `API_BASE` and `API_KEY`.

### Configuration and secrets
All configuration comes from environment variables, or from a `.env` file in the working directory. See `.env.example` and the table in the README.

| Variable | Secret? | Notes |
|---|---|---|
| `API_KEY` | **Yes** | Required, or all protected endpoints return 401. Clients (the UI and scripts) need the same value. |
| `ANTHROPIC_API_KEY` | **Yes** | Optional. Without it, or without credit, insights are computed locally. |
| `INSIGHTS_PROVIDER` | No | `auto` (default) or `rules`. Set `rules` to guarantee no outbound Anthropic calls. |
| `FX_API_URL`, `FX_TIMEOUT_SECONDS` | No | Default `https://api.frankfurter.dev/v1/latest`, 3 s |
| `DATABASE_URL` | No | Use an absolute SQLite path |
| `API_BASE` | No | UI only |

- **When settings are read:**
  - `.env` is loaded once, at startup, so changes need a **restart**.
  - `API_KEY` and the FX settings are read from the process environment on each request.
  - `DATABASE_URL` is read at import time.
- **Never commit `.env`;** it's in `.gitignore`. In production, inject real environment variables from your secret store instead of shipping a `.env` file.
- **Rotating `API_KEY`:** change it, restart the API, and update every client, including the UI's `API_KEY`. There's no grace period and no support for more than one key.

### Network
- **Inbound:** the API port, plus the UI port if you deploy the UI.
- **Outbound HTTPS:**
  - `api.frankfurter.dev`, or whatever `FX_API_URL` points to
  - `api.anthropic.com`, unless `INSIGHTS_PROVIDER=rules`
- **No CORS middleware.** Browsers can't call the API cross-origin. The Streamlit UI isn't affected, because it calls the API from its server process.
- **No TLS** in the app. Terminate TLS at a reverse proxy. The API key travels in a header, so never expose it over plain HTTP.

### Database
- **SQLite, one file.** On startup, `init_db()` runs `create_all()`, which creates missing tables but **never alters existing ones**.
- **There's no migration tool.** A schema change against an existing database needs a manual migration. This already caused a production-style incident in development: a column renamed in the model (`original_currency` → `currency`) left the old table in place, and every insert returned 500 until the column was renamed by hand.
- **Backups:** copy the `.db` file while the API is stopped, or use SQLite's online backup (`sqlite3 expenseflow.db ".backup backup.db"`).
- **Concurrency:** SQLite allows one writer at a time. Running several Uvicorn workers against one file works, but serialises writes; don't expect write throughput. For more than light use, move to a server database. The models are plain SQLAlchemy, but `DATABASE_URL` handling and `check_same_thread` in `app/db.py` are SQLite-specific.
- **Personal data at rest:** the database stores descriptions and categories *unmasked*, since masking is output-only. Treat the file, and its backups, as containing PII.

### Health and monitoring
- **No health endpoint.** `GET /openapi.json` (200, no database access) shows the process is up. `GET /expenses/recent/0` (200) also touches the database.
- **Logging:** the app configures no logging of its own. Its loggers (`app.insights` etc.) use Python's default handling, so warnings and errors reach stderr and INFO is dropped. Uvicorn logs requests. Worth watching for:
  - `Anthropic API error …` / `Could not reach the Anthropic API …` → `Falling back to rules-based insights`
  - `Invalid insight JSON …`
  - repeated `202` responses from `POST /expenses`, which mean the FX API is failing
- **Stuck conversions:** find them with `SELECT id, currency, fx_attempts, fx_last_error FROM expenses WHERE amount_base_minor IS NULL;`

### Behaviour when a dependency is down

| Dependency down | Effect |
|---|---|
| FX API | Submissions still save and return **202**. Those expenses can't be approved (409) until a later retry succeeds. INR expenses are unaffected. |
| Anthropic API | Insights return `source: "rules"`, computed locally. Nothing else is affected. |
| API down (for the UI) | The UI shows "Can't reach the ExpenseFlow API at …" instead of crashing |

---

## Known limitations and risks

1. **The slow-Anthropic risk.** The insights call uses the Anthropic SDK defaults: a 600-second read timeout and 2 retries (SDK 1.8.0), and insights makes up to 2 attempts. A hanging Anthropic API can therefore tie up a request, and a worker thread, for a very long time. Before production, set an explicit timeout on the client in `app/insights.py`, or run with `INSIGHTS_PROVIDER=rules`.
2. **Synchronous outbound calls in the request path.** Submit waits up to `FX_TIMEOUT_SECONDS` on the FX API. Under load, these waits use up the thread pool.
3. **Single shared API key.** No user identity, no roles and no audit of who approved what. `submitted_by` is whatever the client sends.
4. **No rate limiting.** `GET /expenses/insights` can cost Anthropic spend on every call; the key requirement is the only guard.
5. **No pagination** on `GET /expenses`: it returns every row.
6. **Rows created before FX conversion existed** hold unconverted amounts in `amount_base_minor`, e.g. USD cents treated as paise, and are never retried. Fix or delete them before relying on totals.
7. **`GET /expenses/recent/{n}` and `GET /expenses/insights`** were added after the original brief. They're documented in the README and ARCHITECTURE.md.
8. **The currency decimal-places table is hard-coded.** `_CURRENCY_EXPONENTS` in `app/schemas.py` lists currencies with 0 or 3 decimals; every other currency is assumed to have 2. A new unusual currency needs an entry there, or its amounts are converted wrongly. See [ADR 0001](adr/0001-money-as-integer-minor-units.md).

---

## Verifying a deployment

1. **Run the tests:** `python -m pytest -q` should pass all 56. They're offline, use temporary databases, and never call real external APIs.
2. **Check the process is up:** `GET /openapi.json` → 200.
3. **Check auth is active:** `POST /expenses` without `X-API-Key` → 401 `{"detail": "missing or invalid X-API-Key header"}`.
4. **Check FX:** `POST /expenses` with a USD amount and a valid key → 201 with `amount_base_display` set. A 202 means the FX API isn't reachable from the host.
5. **Check insights:** `GET /expenses/insights` with the key → 200. `source: "ai"` means Anthropic is reachable and funded; `source: "rules"` means it isn't, or `INSIGHTS_PROVIDER=rules`.
