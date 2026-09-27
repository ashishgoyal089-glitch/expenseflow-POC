# ExpenseFlow: Production Gap Audit

**Scope:** the code as it stands. That's `app/`, `ui/app.py` and `tests/`, run with 54 tests passing.
**Bar:** a production service handling real employees' expense data, with more than one user and approvals that someone may be held to.

## How to read this

Each gap is classified as:
- **Blocking:** must be fixed before real users or real data.
- **Deferrable:** acceptable at launch, with a plan to fix it.

Effort is rough, for one engineer who knows the codebase:
- **S:** up to 1 day
- **M:** 2–5 days
- **L:** 1–3 weeks

Every gap below comes with evidence taken from the code or from running it. *Probed* means I sent real requests to the app, on a throwaway database.

---

## Summary

| Area | Verdict | Blocking gaps | Blocking effort |
|---|---|---|---|
| 1. Authentication and key rotation | ❌ Not ready | No user identity or roles; reads unauthenticated; rotation needs downtime | L |
| 2. Input validation | ❌ Not ready | No length limits (5 MB description accepted); unbounded list | S |
| 3. Rate limiting | ❌ Not ready | None anywhere; insights spends money per call | S–M |
| 4. Observability and logging | ❌ Not ready | No log config, no request IDs, no audit trail | M |
| 5. Error handling | ❌ Not ready | Plain-text 500s; 600 s Anthropic timeout | S |
| 6. Database migrations and pooling | ❌ Not ready | No migrations; SQLite single-writer; no backups | L |
| 7. Secrets management | ⚠️ Partial | Secrets only in `.env` / plain environment, no store, no rotation process | M |
| 8. Tests and coverage | ⚠️ Partial | No CI; core approve-retry and FX-error paths untested | S–M |
| 9. Deployment and health checks | ❌ Not ready | No container, no health or readiness endpoint, no TLS, no pipeline | M |
| 10. Data privacy | ❌ Not ready | PII stored in plain text; no retention or deletion; third-party sharing unreviewed | M (+ legal) |

**Rough total to clear the blocking gaps: 6–9 engineer-weeks.** Most of it is identity (1), moving to a server database with migrations (6), and building a deployment pipeline (9). Some items overlap, e.g. auth and the audit trail.

**What's already solid:**
- **Money handling:** integer minor units, `Decimal`, one half-up rounding; see [ADR 0001](adr/0001-money-as-integer-minor-units.md).
- **Database `CHECK` constraints.**
- **Safe decisions:** idempotent, race-safe approve and reject.
- **FX outages degrade gracefully** (202, retry later).
- **Keys:** fail-closed API-key check, compared as bytes in constant time.
- **Output-side PII masking.**
- **Insights never block on Anthropic:** there's a local fallback, and injection-resistant prompting.
- **Offline test suite:** fully isolated, 86% line coverage.

---

## 1. Authentication and key rotation

**Current state:** one shared `API_KEY`, sent in `X-API-Key`. It's required for `POST /expenses`, approve, reject and `GET /expenses/insights`. It fails closed if unset (`app/routes.py`, `require_api_key`).

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No user identity.** Anyone with the key can approve anything; `submitted_by` is free text from the client, so approvals can't be attributed. | `submitted_by: str = Field(min_length=1)` in `ExpenseCreate`; no user model | **Blocking** | L (SSO/OIDC via an identity provider, and passing the user through to routes) |
| **No roles or separation of duties.** A submitter can approve their own expense. | `_decide()` checks status only | **Blocking** | M (after identity) |
| **Reads are unauthenticated.** `GET /expenses`, `/expenses/{id}` and `/expenses/recent/{n}` return every expense to anyone who can reach the port. | No `dependencies=[Depends(require_api_key)]` on the GET routes | **Blocking** | S |
| **Rotating the key needs downtime.** Only one key is accepted, so the server and every client must change at the same moment. | `os.getenv("API_KEY")`, compared against a single value | **Blocking** (until identity replaces it) | S (accept a comma-separated list of current and next key) |
| **No key expiry, scoping or per-client keys.** | — | Deferrable (superseded by identity) | M |

## 2. Input validation

**Current state:**
- pydantic models with strict integer amounts (1 to 10^12) and a 3-letter currency pattern.
- Non-blank text fields, with surrounding whitespace trimmed.
- Database `CHECK` constraints back these up.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No maximum length** on `description`, `category`, `submitted_by` or `reason`. | Probed: a **5,000,000-character description returned 201** and was stored in full | **Blocking** | S (`max_length` on fields, plus a request-body size limit at the proxy) |
| **`GET /expenses` is unbounded.** It returns every row, with no pagination. | `list(db.scalars(query))` | **Blocking** (memory and denial-of-service risk as data grows) | S–M (`limit` / `offset` or cursor paging; the UI needs updating too) |
| **Currency isn't checked against ISO 4217.** Any 3 letters pass validation and only fail at the FX call. | Probed: `currency: "XYZ"` → **202**, saved unconverted forever | Deferrable | S (allow-list) |
| **Unknown fields are silently ignored,** which hides client bugs. | Probed: `{"is_admin": true, ...}` → 201 | Deferrable | S (`extra="forbid"`) |
| **No category vocabulary.** Free text makes reports and filters unreliable, e.g. `Travel` vs `travel`. | `category: str` | Deferrable | S–M |

## 3. Rate limiting

**Current state:** none. There's no middleware, and nothing at the proxy level because there's no proxy.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No rate limits on any endpoint.** API-key guessing and write floods are unthrottled. | No limiter anywhere in `app/` | **Blocking** | S (at a reverse proxy or API gateway) to M (in-app per-key limits) |
| **Insights can run up Anthropic spend.** Each call makes up to 2 requests to Claude, carrying up to 200 expenses. | `MAX_ATTEMPTS = 2`, `MAX_INSIGHT_EXPENSES = 200` | **Blocking** (cost) | S (per-key limit, plus caching the result for N minutes) |
| **No spend cap on the Anthropic account.** | Configured outside the code | Deferrable | S (set limits in the Anthropic Console) |

## 4. Observability and logging

**Current state:**
- The app configures no logging. The `app.*` loggers fall back to Python's defaults, so WARNING and above reach stderr and INFO is dropped.
- Uvicorn logs requests.
- There are no metrics or tracing.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No logging configuration or structured (JSON) logs,** so logs can't be searched or turned into alerts. | No `logging.basicConfig` / `dictConfig` in `app/` or `ui/` | **Blocking** | S |
| **No request or correlation IDs.** A client's 500 can't be matched to a server log line. | Probed: a 500 response has no ID header | **Blocking** | S (middleware that sets and logs `X-Request-ID`) |
| **No audit trail for decisions.** `decided_at` records *when*, but not *who*, and nothing is kept for later reading. | `Expense` has no `decided_by`; no audit table | **Blocking** (approvals must be attributable) | M (with identity, see 1) |
| **No metrics:** request rate, latency, FX failure rate, how often insights fall back, and the backlog of stuck conversions. | — | Deferrable | M |
| **No alerting** on FX outages or Anthropic failures. Today they only show up as log lines. | `logger.error("Anthropic API error ...")` | Deferrable | S–M (after metrics) |

## 5. Error handling

**Current state:**
- Expected errors are explicit: 404, 409, 422, 401, and 202 for FX failures.
- FX errors are caught narrowly (`httpx.HTTPError`, `InvalidURL`, JSON `ValueError`, `InvalidOperation`), and so are Anthropic SDK errors. Anthropic failures fall back to local insights.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **Unhandled errors return plain-text 500s.** The body is `Internal Server Error`, with no JSON body and no request ID. | Probed: forced exception → `500 'Internal Server Error'`, `text/plain` | **Blocking** | S (global exception handler returning JSON with a request ID, and logging the traceback) |
| **Anthropic calls can hang for about 10 minutes.** The SDK defaults are `read=600s` and `max_retries=2`, and insights makes up to 2 attempts, so one request can tie up a worker thread. | `anthropic.Anthropic()` with no timeout; SDK 1.8.0 defaults checked | **Blocking** | S (e.g. `Anthropic(timeout=10, max_retries=1)`) |
| **FX is called in the request path.** Submit, get and approve wait up to `FX_TIMEOUT_SECONDS` (3 s) per call, which uses up threads under load. | `httpx.get(...)` inside `try_convert()` | Deferrable | M (background conversion job; the API returns 202 immediately) |
| **Stuck conversions are only retried when someone opens the expense.** Nobody retries them in the background. | Retry only in `get_expense` and `_decide` | Deferrable | M (same job as above) |
| **SQLite `database is locked` errors under concurrent writes surface as 500s.** | SQLite single-writer | Deferrable (goes away with gap 6) | — |

## 6. Database migrations and pooling

**Current state:**
- SQLite, one file. `init_db()` runs `create_all()`, which only creates missing tables.
- SQLAlchemy uses its default `QueuePool`: 5 connections plus 10 overflow.
- The default `DATABASE_URL` is a relative path.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No migrations.** `create_all()` never alters existing tables. | This already broke development: renaming `original_currency` → `currency` caused a 500 on every insert until the column was renamed by hand | **Blocking** | M (Alembic, a baseline migration, and a migrate step at deploy time) |
| **SQLite can't run a multi-instance service.** One writer at a time, no replication or high availability, and a local file tied to one host. | `create_engine("sqlite:///...", check_same_thread=False)` | **Blocking** | L (move to PostgreSQL, run the tests against it, move the data) |
| **No backups or tested restore.** | Nothing in the repo | **Blocking** | S–M (scheduled backups, plus a restore drill) |
| **Existing rows hold wrong INR amounts.** Rows saved before FX existed have USD cents in the INR paise column, and nothing retries them. | `amount_base_minor` copied as-is by the old code | **Blocking** (wrong totals and approvals) | S (one-off data fix) |
| **Connection pool isn't tuned.** 5 + 10 connections against the thread pool's default of 40 worker threads, and no `pool_pre_ping`. | `QueuePool`, size 5 | Deferrable (tune after moving to PostgreSQL) | S |
| **Default database path is relative,** so the file lands in whatever directory the server starts from. | `sqlite:///expenseflow.db` | Deferrable (config only) | S |

## 7. Secrets management

**Current state:**
- Secrets (`API_KEY`, `ANTHROPIC_API_KEY`) come from environment variables or a `.env` file loaded by python-dotenv.
- `.env` is in `.gitignore`, and there are no hard-coded secrets.
- The UI reads the same `API_KEY` from the project `.env`.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No secret store.** Secrets live in a plain-text `.env` on disk, or in the process environment. | `load_dotenv()` in `app/main.py`, `app/insights.py` and `ui/app.py` | **Blocking** | M (inject from the platform's secret manager; stop shipping `.env`) |
| **No rotation process,** for the API key (see 1) or the Anthropic key. | — | **Blocking** | S (documented runbook, plus multi-key support from 1) |
| **Secrets are only read at startup.** `.env` is loaded once, so rotating needs a restart. | `load_dotenv()` at import | Deferrable | S |
| **The UI holds a full-power key.** Anyone who can use the UI can approve. | `API_KEY` in the UI process | Deferrable (resolved by identity, 1) | — |

## 8. Tests and coverage

**Current state:**
- 54 tests, fully offline: temporary SQLite databases, FX faked at the httpx transport level, and a stubbed Anthropic client.
- The UI is tested headlessly with pandas made unimportable.
- **86% line coverage** (574 statements, 78 missed), measured with `coverage` installed in a scratch folder; it isn't a project dependency.

| File | Coverage |
|---|---|
| `app/models.py`, `app/schemas.py`, `app/sanitize.py` | 100% |
| `app/insights.py` | 90% |
| `app/routes.py` | 89% |
| `app/main.py` | 85% |
| `ui/app.py` | 75% |
| `app/db.py` | 62% (bypassed on purpose by the test database override) |

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No CI.** Tests only run when someone remembers to run them locally. | No pipeline config in the repo | **Blocking** | S |
| **The core approve path is untested:** approving an *unconverted* expense, retry success, and the 409 on retry failure. | `app/routes.py` lines 160–167 never run | **Blocking** | S |
| **FX bad-response handling is untested:** non-JSON body, missing rate, non-numeric or zero rate, and a result that rounds to 0 paise. | `app/routes.py` lines 85–97 and 122 never run | **Blocking** (this is exactly the degradation logic production relies on) | S |
| **The concurrent-approval race is untested,** as is the 409 when another approver wins. | Line 181 never runs | Deferrable | S–M |
| **The category filter is untested.** | Line 229 never runs | Deferrable | S |
| **No coverage tooling or threshold** in the project. | `coverage` / `pytest-cov` not in `requirements.txt` | Deferrable | S |
| **No tests against the production database** (PostgreSQL, after gap 6), and no load tests. | SQLite only | Deferrable, until gap 6 lands | M |

## 9. Deployment and health checks

**Current state:**
- Run by hand with `python -m uvicorn app.main:app --reload`.
- The Streamlit UI is a second process.
- On Windows with Smart App Control, installing needs `DISABLE_SQLALCHEMY_CEXT=1`, and pandas can't load.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **No container or reproducible build.** | No Dockerfile or compose file in the repo | **Blocking** | M |
| **No health or readiness endpoint.** Only `/openapi.json` is usable, and it doesn't check the database or dependencies. | No `/health` route | **Blocking** | S (`/healthz` for liveness; `/readyz` that checks the database) |
| **No TLS.** The API key would travel in plain text. | Uvicorn serves plain HTTP | **Blocking** | S (terminate TLS at a reverse proxy or load balancer) |
| **No build and deploy pipeline,** and no environments (dev, staging, prod). | — | **Blocking** | M |
| **The dev-mode command is the documented one.** `--reload` must not run in production. | README and CLAUDE.md run command | Deferrable (document a production command with `--workers`) | S |
| **No CORS policy.** Fine for the server-side Streamlit UI, but it blocks any future browser client. | No CORS middleware | Deferrable | S |
| **The Windows Smart App Control workarounds** tie the build to that environment's quirks. | `requirements.txt` header, `--no-binary sqlalchemy` | Deferrable (a Linux container avoids it) | — |

## 10. Data privacy for expense data

**Current state:**
- Emails, phone numbers and card or account numbers are masked in `description` and `category` in every API response.
- The same text is masked before being sent to Anthropic, capped at 200 expenses, and marked as untrusted data.
- Rules-based insights never quote descriptions.

| Gap | Evidence | Class | Effort |
|---|---|---|---|
| **PII is stored in plain text.** Masking is output-only, and there's no encryption at rest. The database file and every backup contain raw card numbers, phone numbers and emails. | Probed in the earlier masking work: the stored row keeps the original | **Blocking** | S–M (disk or database encryption at rest; consider masking or tokenising card numbers *on write*, as there's no business need to keep them) |
| **No retention or deletion.** There's no delete endpoint and no retention period, so an individual's data can't be erased. India's Digital Personal Data Protection Act, 2023 may require this; confirm with legal. | No DELETE route; no retention job | **Blocking** | M (policy, plus a delete or anonymise capability) |
| **Expense text goes to a third party (Anthropic) without a recorded decision.** Masking is regex-based, so names, addresses and ID numbers it doesn't recognise still leave the system. | `_expense_data_block()` sends descriptions and categories | **Blocking** (a legal or processing decision, not engineering) | S engineering (keep `INSIGHTS_PROVIDER=rules` until approved) + legal review |
| **`submitted_by` isn't masked,** and it's often a name or email. | Probed: `"bob@corp.com 9845723112"` returned as-is | Deferrable (decide whether it's needed at all once identity exists) | S |
| **Masking is pattern-based.** It misses names, addresses, government IDs and numbers split across unusual separators. | `app/sanitize.py` handles only email, phone and card/account formats | Deferrable | M (named-entity based detection, or restricting free text) |
| **Access to expense data isn't logged or restricted** beyond the shared key. | Reads are unauthenticated (see 1) | **Blocking** (covered by 1 and 4) | — |

---

## Suggested order of work

1. **Quick safety fixes (about 1 week, all S):**
   - input length limits and pagination
   - authenticated reads
   - Anthropic client timeout
   - global JSON error handler with request IDs
   - logging config
   - health endpoints
   - CI with the missing core-path tests
   - multi-key rotation
   - the one-off INR data fix
   - `INSIGHTS_PROVIDER=rules` until the Anthropic data-sharing decision is made
2. **Platform (2–3 weeks):**
   - PostgreSQL with Alembic migrations, and backups with a restore drill
   - container and deploy pipeline with TLS
   - secret manager
   - rate limiting at the gateway
3. **Identity and compliance (2–4 weeks):**
   - SSO/OIDC users and roles
   - separation of duties
   - decision audit trail
   - retention and deletion
   - encryption at rest and masking card numbers on write
4. **After launch:** metrics and alerting, background FX conversion, ISO currency allow-list, better PII detection, load tests.
