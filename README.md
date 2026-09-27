# ExpenseFlow

A small expense submission and approval API, built as a proof of concept (not production).

It supports one journey: **submit an expense in any currency, have it converted to Indian rupees (INR), then approve or reject it.** On top of that it has:
- spending insights, written by Claude when the Anthropic API is available and computed locally when it isn't
- personal-data masking in every response
- a Streamlit web UI

**Stack:** Python 3.14, FastAPI, Uvicorn, SQLAlchemy on SQLite, pydantic v2, httpx, python-dotenv, the Anthropic SDK, Streamlit and pytest.

---

## What it does

- **Submit expenses** in any currency. Amounts are sent as integer *minor units* (cents, paise, …), never floats.
- **Converts to INR when saved,** using a live exchange rate from [Frankfurter](https://frankfurter.dev) (free, no key).
  - The conversion uses `Decimal` arithmetic, rounds half-up once to whole paise, and stores the rate used as a string.
  - INR expenses skip the exchange-rate call.
- **Still saves when the exchange-rate service is down.** The expense is stored with no INR amount, and the API returns `202`. The conversion is retried on the next `GET` or approve.
- **Approves or rejects,** with safe repeats:
  - Repeating the same decision is a no-op.
  - Changing a decision is refused with `409`.
  - An expense can't be approved until it has an INR amount; the database enforces this too.
- **Insights:** a one-line summary and three bullet points about spending.
  - Written by Claude (`claude-sonnet-4-6`) when the Anthropic API works.
  - Otherwise computed locally from the real numbers.
- **Masks personal data.** Emails, phone numbers and card or account numbers in `description` and `category` are shown as `[EMAIL]`, `[PHONE]` and `[CARD]` in every response. The database keeps the original text.
- **Protects writes with an API key.** Writes and insights need an `X-API-Key` header. Other reads are open.
- **Web UI** (`ui/app.py`):
  - a form to submit expenses
  - a table of expenses, with amounts in rupees and status labels
  - a *Generate insights* button

INR amounts are shown with the ₹ symbol and Indian lakh/crore grouping, e.g. `₹1,25,000.00`.

---

## Project layout

| Path | Contents |
|---|---|
| `app/main.py` | Creates the FastAPI app, loads `.env`, creates tables on startup, mounts the routes |
| `app/routes.py` | All endpoints, API-key check, exchange-rate client and `try_convert()` |
| `app/schemas.py` | Request and response models, and money helpers (currency decimals, INR conversion, ₹ formatting) |
| `app/models.py` | The `Expense` table and its database constraints |
| `app/db.py` | Database engine, sessions, `get_db` dependency, `init_db()` |
| `app/insights.py` | Insights, from Claude or computed locally |
| `app/sanitize.py` | Personal-data masking (`mask_pii`, `mask_expense`) |
| `ui/app.py` | Streamlit web UI |
| `tests/` | pytest suite: `test_api.py`, `test_insights.py`, `test_sanitize.py`, `test_ui.py` |
| `scripts/seed_attack.py` | Submits one expense whose description is a prompt-injection attempt, for testing insights |
| `docs/ARCHITECTURE.md` | Design decisions: schema, lifecycle, edge cases |
| `.env.example` | Template for your `.env` |

---

## Setup on Windows

You need **Python 3.14** and **PowerShell**. Run every command from the project folder.

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
```

If PowerShell refuses to run the activation script, allow it for the current window only, then activate again:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.venv\Scripts\Activate.ps1
```

### 2. Install the dependencies

```powershell
$env:DISABLE_SQLALCHEMY_CEXT='1'; .venv\Scripts\python.exe -m pip install -r requirements.txt
```

**Why the extra variable:** on machines with **Windows Smart App Control**, Windows blocks SQLAlchemy's compiled extensions. The import then fails with `DLL load failed ... An Application Control policy has blocked this file`. `requirements.txt` builds SQLAlchemy from source (`--no-binary sqlalchemy`), and `DISABLE_SQLALCHEMY_CEXT=1` makes that build pure Python. You need both, and pip can't set the variable from the requirements file.

To check it worked, run this. It should print `0`, meaning no compiled SQLAlchemy files:

```powershell
(Get-ChildItem .venv\Lib\site-packages\sqlalchemy -Recurse -Filter *.pyd).Count
```

> **pandas is blocked too.** Streamlit installs pandas, and Smart App Control also blocks pandas's compiled extension. The UI therefore avoids pandas-based Streamlit elements, such as `st.dataframe`, and draws its table as Markdown. `tests/test_ui.py` runs the UI with pandas made unimportable, to catch any regression.

---

## Configuration

Settings are read from environment variables. `.env` in the project folder is loaded automatically, through python-dotenv. Create yours from the template:

```powershell
Copy-Item .env.example .env
```

Then generate an API key and paste it into `API_KEY=` in `.env`:

```powershell
.venv\Scripts\python.exe -c "import secrets; print(secrets.token_urlsafe(24))"
```

| Variable | Used by | Default if unset | Purpose |
|---|---|---|---|
| `API_KEY` | API, UI, `scripts/seed_attack.py` | *(none)* | Shared key that clients send in `X-API-Key`. **If unset, every protected request is refused with 401.** |
| `ANTHROPIC_API_KEY` | API (insights) | *(none)* | Key for Claude insights. Optional: without it, or without credit, insights are computed locally. |
| `INSIGHTS_PROVIDER` | API (insights) | `auto` | `auto` tries Claude, then falls back to local insights. `rules` always computes locally, with no API call. |
| `FX_API_URL` | API | `https://api.frankfurter.dev/v1/latest` | Exchange-rate endpoint, called as `GET {FX_API_URL}?from=USD&to=INR`. It must return `{"rates": {"INR": <rate>}}`. |
| `FX_TIMEOUT_SECONDS` | API | `3` | Timeout for the exchange-rate call. |
| `DATABASE_URL` | API | `sqlite:///expenseflow.db` | SQLAlchemy database URL. The default file is created in the folder you start the server from. |
| `API_BASE` | UI | `http://127.0.0.1:8000` | Where the UI finds the API. |

`.env` is listed in `.gitignore`. Never commit it.

> **After editing `.env`, restart the server.** `.env` is read once, at startup. `--reload` only restarts on `.py` changes.

---

## Running

### API server

```powershell
python -m uvicorn app.main:app --reload
```

- The API runs at **http://127.0.0.1:8000**.
- Interactive docs (Swagger UI) are at **http://127.0.0.1:8000/docs**. Click **Authorize** and enter your `API_KEY` to call the protected endpoints.
- ReDoc is at `/redoc`, and the OpenAPI schema is at `/openapi.json`.
- On startup, the `expenses` table is created if it doesn't exist. An existing table is left unchanged.

### Web UI

With the API running, in a second PowerShell window with the virtual environment activated:

```powershell
streamlit run ui/app.py
```

Streamlit prints the URL, usually **http://localhost:8501**.

### Tests

```powershell
python -m pytest -q
```

There are 54 tests across `test_api.py`, `test_insights.py`, `test_sanitize.py` and `test_ui.py`. They are self-contained:
- **Their own database:** each API test gets a new temporary SQLite database, so your `expenseflow.db` is never touched.
- **No network:** the exchange-rate API is faked at the httpx transport level, and the Anthropic client is always replaced by a stub. No test calls the network or spends API credit.
- **No running server:** the UI tests run the Streamlit app headlessly (`AppTest`) against a faked API.

---

## Authentication

These requests need an `X-API-Key` header equal to `API_KEY`:

- `POST /expenses`
- `POST /expenses/{id}/approve`
- `POST /expenses/{id}/reject`
- `GET /expenses/insights`

A missing or wrong key returns:

```json
401 {"detail": "missing or invalid X-API-Key header"}
```

Other `GET` endpoints are open. It's one shared key for all clients, so it proves the caller may use the protected endpoints, not *who* they are. `submitted_by` is whatever the client sends.

---

## Endpoint reference

Base URL: `http://127.0.0.1:8000`. All bodies are JSON.

| Method | Path | Auth | Summary |
|---|---|---|---|
| POST | `/expenses` | `X-API-Key` | Submit an expense |
| GET | `/expenses` | none | List expenses, optionally filtered |
| GET | `/expenses/insights` | `X-API-Key` | Summary and three insight bullets |
| GET | `/expenses/recent/{n}` | none | The `n` newest expenses |
| GET | `/expenses/{id}` | none | One expense |
| POST | `/expenses/{id}/approve` | `X-API-Key` | Approve a pending expense |
| POST | `/expenses/{id}/reject` | `X-API-Key` | Reject a pending expense |

### The expense object (`ExpenseOut`)

Every endpoint that returns an expense uses this shape:

| Field | Type | Meaning |
|---|---|---|
| `id` | int | Expense id |
| `description` | string | As submitted, **with personal data masked** |
| `amount_minor` | int | Amount as submitted, in minor units of `currency` |
| `currency` | string | 3-letter ISO 4217 code, upper case |
| `category` | string | As submitted, **with personal data masked** |
| `submitted_by` | string | As submitted |
| `amount_base_minor` | int \| null | Amount in INR paise. `null` until the conversion succeeds |
| `amount_base_display` | string \| null | INR amount formatted like `"₹12,025.41"`, or `null` while unconverted |
| `fx_rate` | string \| null | Rate used (INR per 1 unit), as a decimal string. `"1"` for INR |
| `fx_rate_at` | datetime \| null | When the rate was fetched (UTC) |
| `fx_attempts` | int | How many times conversion has been tried. Stays `0` for INR |
| `fx_last_error` | string \| null | Why the last conversion attempt failed |
| `status` | string | `pending`, `approved` or `rejected` |
| `decision_reason` | string \| null | Reason given on approve or reject |
| `created_at` | datetime | When it was submitted (UTC) |
| `decided_at` | datetime \| null | When it was first approved or rejected. It's never overwritten |

### POST `/expenses`

Submits an expense. It's saved as `pending` and converted to INR.

**Request body:**

| Field | Rules |
|---|---|
| `description` | string, required, not blank (surrounding whitespace is trimmed) |
| `amount_minor` | **integer** in minor units, from 1 to 10^12. Floats and numeric strings are rejected. Examples: `12550` = $125.50, `1500` = ¥1,500 |
| `currency` | 3 letters. Lower case is accepted and upper-cased. |
| `category` | string, required, not blank |
| `submitted_by` | string, required, not blank |

**Decimal places by currency:** most currencies use 2. These use 0: JPY, KRW, VND, CLP, ISK, PYG, UGX, RWF, BIF, DJF, GNF, KMF, VUV, XAF, XOF, XPF. These use 3: KWD, BHD, OMR, JOD, TND, IQD, LYD.

**Responses:**

| Code | When |
|---|---|
| **201** | Saved and converted to INR |
| **202** | Saved, but the exchange-rate call failed: `amount_base_minor` is `null` and `fx_last_error` says why. Retried on the next `GET /expenses/{id}` or approve. |
| **401** | Missing or wrong `X-API-Key` |
| **422** | Invalid body |

Example: `{"description": "Flight to Bangalore", "amount_minor": 12550, "currency": "usd", "category": "travel", "submitted_by": "alice"}` returns **201**:

```json
{
  "id": 1,
  "description": "Flight to Bangalore",
  "amount_minor": 12550,
  "currency": "USD",
  "category": "travel",
  "submitted_by": "alice",
  "amount_base_minor": 1202541,
  "fx_rate": "95.82",
  "fx_rate_at": "2026-09-27T12:54:34.962702Z",
  "fx_attempts": 1,
  "fx_last_error": null,
  "status": "pending",
  "decision_reason": null,
  "created_at": "2026-09-27T12:54:34.554634Z",
  "decided_at": null,
  "amount_base_display": "₹12,025.41"
}
```

When the exchange-rate service is unreachable, you get **202** instead. The same fields come back, with:

```json
"amount_base_minor": null, "fx_rate": null, "fx_attempts": 1,
"fx_last_error": "ConnectError: unreachable", "amount_base_display": null
```

A float amount returns **422**:

```json
{"detail": [{"type": "int_type", "loc": ["body", "amount_minor"], "msg": "Input should be a valid integer", "input": 125.5}]}
```

PowerShell example:

```powershell
Invoke-RestMethod -Method Post http://127.0.0.1:8000/expenses `
  -Headers @{"X-API-Key" = "<your API_KEY>"} -ContentType "application/json" `
  -Body '{"description":"Taxi","amount_minor":45000,"currency":"INR","category":"travel","submitted_by":"alice"}'
```

### GET `/expenses`

Lists expenses, oldest first (by `id`). There's no pagination.

**Query parameters,** both optional and combinable:

| Parameter | Effect |
|---|---|
| `status` | `pending`, `approved` or `rejected`. Any other value returns **422**. |
| `category` | Exact match on the stored category |

**Response:** **200**, a list of expense objects. Example: `GET /expenses?status=pending`.

### GET `/expenses/{id}`

**Responses:**

| Code | When |
|---|---|
| **200** | The expense object. If it isn't converted to INR yet, the conversion is retried first, which increments `fx_attempts`. |
| **404** | `{"detail": "expense not found"}` |

### POST `/expenses/{id}/approve`

**Request body:** optional, e.g. `{"reason": "within policy"}`. The body can be omitted.

**Responses:**

| Current status | Result |
|---|---|
| `pending`, converted | **200**: becomes `approved`, and `decision_reason` and `decided_at` are set |
| `pending`, not converted | The conversion is retried. If it succeeds, **200** approved; otherwise **409** `{"detail": "cannot approve until converted to INR"}` |
| `approved` | **200** no-op: the stored record comes back unchanged |
| `rejected` | **409** `{"detail": "already rejected"}` |
| missing | **404** `{"detail": "expense not found"}` |

A missing or wrong key returns **401**.

### POST `/expenses/{id}/reject`

**Request body:** required, `{"reason": "..."}`, where `reason` must not be blank. Without it you get **422**, with `"loc": ["body", "reason"], "msg": "Field required"`.

**Responses:**

| Current status | Result |
|---|---|
| `pending` | **200**: becomes `rejected`. An INR amount isn't needed. |
| `rejected` | **200** no-op |
| `approved` | **409** `{"detail": "already approved"}` |
| missing | **404** `{"detail": "expense not found"}` |

A missing or wrong key returns **401**.

### GET `/expenses/insights`

Returns a one-sentence summary and three bullets about the **200 newest** expenses.

```json
{
  "insight": {
    "summary": "Total spending is ₹13,275.91 across 2 expenses, with ₹1,250.50 (9%) still pending approval.",
    "bullets": [
      "travel is the largest category at ₹12,025.41 (91% of spend) from 1 expense, across 2 categories in all.",
      "₹12,025.41 is approved, ₹1,250.50 is pending and ₹0.00 is rejected.",
      "Expenses range from ₹1,250.50 to ₹12,025.41, averaging ₹6,637.96."
    ],
    "source": "rules"
  }
}
```

- **`source: "ai"`:** written by Claude. Descriptions and categories are sent with personal data masked. They're wrapped in an `<expense_data>` block that the model is told to treat as untrusted data, never as instructions.
- **`source: "rules"`:** computed locally, because the Anthropic API failed (no key, no credit, network, or invalid replies) or because `INSIGHTS_PROVIDER=rules`.
  - The bullets cover the largest category and its share, then the approved, pending and rejected amounts.
  - The third bullet reports possible duplicates if there are any; otherwise expenses still awaiting conversion; otherwise the range and average.
  - Only converted expenses count towards INR totals, and descriptions are never quoted.
- **No expenses:** `summary` is `"No expenses to analyse yet."` and `bullets` is `[]`.

A missing or wrong key returns **401**.

### GET `/expenses/recent/{n}`

Returns the `n` newest expenses (by `created_at`), plus the newest one's description in upper case (masked, like every description).

- `n` must be an integer; otherwise you get **422**.
- `n` of 0 or less returns no items.

```json
{
  "latest": "TEAM LUNCH, CARD [CARD]",
  "items": [ { "id": 2, "description": "Team lunch, card [CARD]", "...": "full expense object" } ]
}
```

With no expenses, the response is `{"latest": null, "items": []}`.

---

## Data rules worth knowing

- **Money is never a float.** It's stored as integer minor units, converted with `Decimal`, and rounded half-up once to whole paise.
- **Database checks:**
  - `amount_minor > 0`
  - `amount_base_minor` is `NULL` or greater than 0
  - `status` is one of the three values
  - an `approved` row must have an `amount_base_minor`
- **Masking is output-only.** `expenseflow.db` still holds the original descriptions and categories, so anyone with the database file can read them.
- **Old rows keep their old INR amounts.** Rows saved before conversion existed still hold their original, unconverted amounts, and nothing retries them. Only rows with `amount_base_minor = NULL` are retried.

More detail on design decisions is in [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).
