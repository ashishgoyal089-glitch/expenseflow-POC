# ADR 0001: Store money as integer minor units

- **Status:** Accepted
- **Scope:** `app/schemas.py`, `app/models.py`, `app/routes.py`, `ui/app.py`

## Context

ExpenseFlow takes amounts in many currencies and converts them to INR (Indian rupees), the base currency. Those amounts are then totalled, approved and shown to people. Money needs to be exact: a total must not drift by a paisa, and a converted amount must be reproducible later for an audit.

Two facts shaped the decision:
- **Binary floats can't represent most decimal amounts.** In Python, `0.1 + 0.2 == 0.30000000000000004`.
- **SQLite has no exact decimal type.** A `NUMERIC(12,2)` column stores `0.1` and `125.50` as `real`, i.e. binary floats. Checked in this project with `typeof()`. SQLAlchemy's `Numeric` returns a `Decimal`, but the value on disk is still a float.

Currencies also differ in how many decimal places they have: JPY 0, USD 2, KWD 3.

## Decision

Store and transfer every amount as an **integer number of minor units** of its currency: cents, paise, fils, or whole yen.

- **API input:**
  - `amount_minor` is a strict JSON integer, from 1 to 10^12.
  - Floats such as `125.5` and numeric strings such as `"12550"` are rejected with a 422 (pydantic `strict=True`).
- **Storage:** `amount_minor` (the original currency) and `amount_base_minor` (INR paise) are `INTEGER` columns. `CHECK` constraints require them to be greater than 0.
- **Conversion:**
  - Done in `Decimal`: `minor × 10^(−exponent) × rate × 100`.
  - Rounded **half-up, once**, to whole paise.
  - The rate is stored as a decimal **string** (`fx_rate`), so any INR amount can be recomputed exactly.
  - The FX API's JSON is parsed with `parse_float=Decimal`, so the rate never passes through a float.
- **Decimal places:** a single `currency_exponent()` table (0, 2 or 3) is used by both the API and the UI.
- **Formatting happens only for display:** `amount_base_display` in API responses and the rupee column in the UI, e.g. `₹1,25,000.00` with Indian digit grouping.

## Alternatives considered

| Option | Why not |
|---|---|
| **Float amounts** (`125.5`) | Inexact, with rounding errors that add up in totals. Rejected outright. |
| **Decimal or `NUMERIC` columns** | SQLite would still store floats underneath (see Context). It would be exact only after moving to another database, and would depend on each driver getting decimals right. |
| **Decimal strings in the API and database** (`"125.50"`) | Exact, but every value needs parsing and validation on every read. Clients could send `"125.5"`, `"125.500"` or `"1e2"`, and number formats differ by locale. Sorting and summing in SQL wouldn't work on strings. |
| **Major units with a fixed 2 decimals** (store rupees × 100 for everything) | Wrong for currencies with 0 or 3 decimals: JPY has no sub-unit, and KWD has 1,000 fils to the dinar. |

## Consequences

**Good:**
- **Exact arithmetic:** sums and comparisons are integer operations, including in SQL.
- **Reproducible conversions:** the original amount, the rate string and the single rounding rule reproduce any INR amount.
- **Invalid values rejected at the edge:** floats, zero and negative amounts are refused by the API and by database constraints.

**Costs and risks:**
- **Clients must convert to minor units** using the right number of decimal places, e.g. $125.50 → `12550`, ¥1,500 → `1500`. The UI does this for users (`to_minor_units`), but other API clients must do it themselves.
- **The exponent table must be maintained by hand.** Currencies not listed are assumed to have 2 decimals, so a new 0- or 3-decimal currency would be converted wrongly until it's added.
- **Integer range is bounded.** SQLite integers top out at 2^63 − 1. Input is capped at 10^12, and a conversion that would overflow is recorded as an FX failure instead of crashing. This was found and fixed after a code review.
- **Raw values are easy to misread.** Anyone reading the API or database directly sees `125050`, not `1,250.50`. Responses therefore also include `amount_base_display`.
