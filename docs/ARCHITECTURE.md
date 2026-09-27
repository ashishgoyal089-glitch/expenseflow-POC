# ExpenseFlow Architecture

A small expense submission and approval API (a PoC, not production). It supports one user journey: **submit an expense, convert it to INR, then approve or reject it.**

**Stack:** Python 3.14, FastAPI, Uvicorn, SQLAlchemy ORM on SQLite (`expenseflow.db`), httpx for the external FX rate call, pydantic v2, python-dotenv and pytest. Dependencies are pinned in `requirements.txt`.

**Money rules** (from `CLAUDE.md`):
- **Storage:** amounts are integer minor units (paise or cents), never floats.
- **Base currency:** INR. Amounts are converted to INR when saved.
- **Display:** INR amounts use the ₹ symbol with Indian lakh/crore grouping, e.g. `₹1,25,000.00`.

---

## 1. Schema: `expenses`

| Column | Type | Why it exists |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | Stable handle for get, approve and reject |
| `submitted_by` | TEXT NOT NULL | Who claims the money. Self-reported: the shared API key (see §2) doesn't identify users |
| `description` | TEXT NOT NULL | What the approver reads to decide |
| `category` | TEXT NOT NULL | What kind of spend it is (e.g. travel, meals), so the approver can judge it against policy |
| `amount_minor` | INTEGER NOT NULL, CHECK > 0 | The amount as submitted, in the original currency's minor units. This is the source of truth, and it's always saved, even when FX is down |
| `currency` | TEXT(3) NOT NULL | ISO 4217 code. Needed to read the amount above and to retry the conversion |
| `amount_base_minor` | INTEGER NULL, CHECK NULL or > 0 | The amount in INR paise. **NULL until the conversion succeeds.** It's never guessed and never 0 |
| `fx_rate` | TEXT NULL | The rate actually used, stored as a decimal string rather than a float so it can be reproduced exactly. It's `"1"` for INR |
| `fx_rate_at` | TEXT NULL | When the rate was fetched (UTC, ISO 8601) |
| `fx_attempts` | INTEGER NOT NULL DEFAULT 0 | How many times the conversion has been tried |
| `fx_last_error` | TEXT NULL | Why the last conversion attempt failed |
| `status` | TEXT NOT NULL DEFAULT `pending`, CHECK IN (`pending`, `approved`, `rejected`) | Where the expense is in its lifecycle |
| `decision_reason` | TEXT NULL | Why it was rejected (required on reject, optional on approve) |
| `created_at` | TEXT NOT NULL, defaults to now | When it was submitted (UTC, ISO 8601) |
| `decided_at` | TEXT NULL | When it was first approved or rejected. It's set once and never overwritten |

**Consistency check between status and INR amount**, enforced by the database:
- `approved` requires `amount_base_minor IS NOT NULL`, so an expense can never be approved without an INR amount.
- `pending` and `rejected` can go either way. A `pending` expense with a NULL `amount_base_minor` is one still waiting for FX; there is no separate status for it.

### Lifecycle

```
submit ──▶ pending ──approve──▶ approved   (final; needs amount_base_minor)
             │  └─────reject──▶ rejected   (final; INR amount optional)
             └─ amount_base_minor NULL while FX is down; filled on a later retry
```

### Conversion
- **Input:** amounts arrive as `amount_minor`, a positive JSON integer in the currency's minor units (e.g. `12550` for $125.50, `1500` for ¥1,500). Zero, negatives, floats and numeric strings are a 422. `currency` must be a 3-letter code; lowercase is accepted and uppercased.
- **Converting:** INR paise = original major units × rate, rounded **half-up once** to whole paise.
- **Bad rates:** a missing, zero, negative or non-numeric rate counts as an FX failure. So does a timeout or HTTP error.
- **INR expenses:** these skip the FX call entirely. The rate is `"1"`, and the INR amount equals the original amount.

---

## 2. Endpoints

These are the only endpoints. The first four are the core brief; the list, insights and recent endpoints were added later on request.

| Method | Path | Request body | Response |
|---|---|---|---|
| POST | `/expenses` | `{description, amount_minor: 12550, currency: "USD", category, submitted_by}` | **201** `ExpenseOut` with `status: pending` if the conversion worked. **202** `ExpenseOut` with `status: pending` and `amount_base_minor: null` if FX failed. **422** for invalid input, including `amount_minor` above 10^12 |
| GET | `/expenses?status=&category=` | none | **200** list of `ExpenseOut`, oldest first, optionally filtered |
| GET | `/expenses/insights` | none | **200** `{insight: {summary, bullets[3], source}}` over the 200 newest expenses. `source: "ai"` when Claude wrote it (PII masked, sent as untrusted data); `source: "rules"` when computed locally from the numbers because the API failed or `INSIGHTS_PROVIDER=rules`. **Needs `X-API-Key`** because AI calls cost API spend |
| GET | `/expenses/recent/{n}` | none | **200** `{latest, items}`: the `n` newest `ExpenseOut`s, and the newest description in upper case (`null` if there are none) |
| GET | `/expenses/{id}` | none | **200** `ExpenseOut`. If `amount_base_minor` is still NULL, the conversion is retried first. **404** if it doesn't exist |
| POST | `/expenses/{id}/approve` | `{reason?: string}` | See the decision rules below |
| POST | `/expenses/{id}/reject` | `{reason: string}` (required) | See the decision rules below |

`ExpenseOut` contains `id, description, amount_minor, currency, category, submitted_by, amount_base_minor, amount_base_display, fx_rate, fx_rate_at, fx_attempts, fx_last_error, status, decision_reason, created_at, decided_at`.

`amount_base_display` is the INR amount formatted like `"₹1,25,000.00"`, or `null` while the expense is still unconverted.

**PII masking:** every response masks emails, phone numbers and card/account numbers in `description` and `category` as `[EMAIL]`, `[PHONE]` and `[CARD]` (see `app/sanitize.py`). The database keeps the original text; masking happens only on the way out.

### Auth
Writes (`POST /expenses`, approve and reject) and `GET /expenses/insights` need an `X-API-Key` header matching the `API_KEY` environment variable. Otherwise they return **401** `"missing or invalid X-API-Key header"` and nothing is changed. It fails closed: if `API_KEY` isn't set, every protected request is refused. Other reads are open. It's one shared key for all clients, so it proves the caller is allowed to use protected endpoints but not *who* they are.

### Decision rules

| Current status | approve | reject |
|---|---|---|
| `pending`, not yet converted | Retry the conversion. If it succeeds, **200** approved. If it fails, **409** "cannot approve until converted to INR" | **200**, becomes rejected |
| `pending`, converted | **200**, becomes approved | **200**, becomes rejected |
| `approved` | **200 no-op**: the stored record comes back unchanged | **409** "already approved" |
| `rejected` | **409** "already rejected" | **200 no-op** |

A missing expense returns **404** for either action.

---

## 3. File layout

| Path | Contents |
|---|---|
| `app/main.py` | Creates the FastAPI app, loads `.env` via python-dotenv, includes the router and creates tables on startup |
| `app/db.py` | The engine (`DATABASE_URL`, default `sqlite:///expenseflow.db`), `SessionLocal`, `Base`, the `get_db` dependency and `init_db()` (creates all tables) |
| `app/models.py` | The `Expense` model, status constants and all CHECK constraints |
| `app/schemas.py` | `ExpenseCreate`, `ApproveIn`, `RejectIn` and `ExpenseOut`, plus the money helpers: currency exponents, conversion with half-up rounding and ₹ lakh/crore formatting |
| `app/routes.py` | The four endpoints (each with a docstring). Also the httpx FX client (configured from env, with a short timeout), the `try_convert()` helper shared by submit, get and approve, and the decision rules |
| `tests/` | pytest tests using a temporary SQLite database and a fake FX client |
| `.env.example` | `API_KEY`, `ANTHROPIC_API_KEY`, `FX_API_URL`, `FX_TIMEOUT_SECONDS`, `DATABASE_URL`. Secrets only ever come from env |
| `requirements.txt` | Pinned dependencies |

**Run:** `python -m uvicorn app.main:app --reload`
**Test:** `python -m pytest -q`

---

## 4. Edge-case decisions

### 4.1 The FX API is down, slow or returns bad data at submit time
**Decision:** save the expense anyway. It isn't rejected, and the INR amount isn't guessed.
- **Saved in full:** the row is saved with `status = pending` and `amount_base_minor = NULL`, and the API returns **202**. The original amount and currency are always kept, and `fx_attempts` and `fx_last_error` record each failure.
- **Retried later:** the conversion runs again on the next `GET /expenses/{id}` or approve call. When it succeeds, `amount_base_minor`, the rate and its fetch time are filled in.
- **Blocked from approval:** an expense that hasn't been converted **can't be approved** (409). The database check enforces this independently of the code.
- **Can still be rejected:** rejecting doesn't need an INR amount.

*Rejected alternatives:* returning a 502 and saving nothing loses the user's submission. Using a cached or default rate could approve a wrong amount.

### 4.2 Approving or rejecting an expense that's already decided
**Decision:** repeating the same decision is a **200 no-op**, and changing a decision is a **409**.
- **Repeats succeed:** a client that retries after a timeout, or a double-click, gets the same successful answer, not an error for something that worked.
- **Decisions are final:** approved and rejected never change after the first decision. `decided_at` and `decision_reason` keep the first decision's values.
- **Racing approvers:** every change is a conditional `UPDATE … WHERE id = ? AND status IN (<allowed from-states>)`. If it changes 0 rows, the handler re-reads the row. If the row now holds the same decision, it returns 200. Otherwise it returns 409. Two approvers acting at once produce one change and two identical 200 responses.

### 4.3 Float and rounding errors in money
**Decision:** use integer storage with `Decimal` arithmetic, and round once.
- **No floats:** amounts are accepted only as integer minor units. A JSON float such as `125.5` is a 422, so float precision can't creep in.
- **Precision per currency:** the client sends minor units, so there are never excess decimal places to reject. The currency's exponent (JPY 0, most currencies 2, KWD 3) is applied only when converting to INR.
- **One rounding step:** the conversion rounds half-up once, to whole paise. The rate is stored as a string, so any INR amount can be recomputed and audited.
