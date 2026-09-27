"""HTTP endpoints for submitting, listing, reading and deciding on expenses.

Also holds the FX client and try_convert(), shared by submit, get and approve. The rate
API is configured from env (FX_API_URL, FX_TIMEOUT_SECONDS); the default is Frankfurter,
which needs no key and returns {"base": "USD", "rates": {"INR": 95.82}}.
"""

import os
import secrets
from decimal import Decimal, InvalidOperation

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Response, Security, status
from fastapi.security import APIKeyHeader
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.db import get_db
from app.insights import generate_insight
from app.sanitize import mask_pii
from app.models import APPROVED, PENDING, REJECTED, Expense, utc_now_iso
from app.schemas import (
    BASE_CURRENCY,
    SQLITE_MAX_INT,
    ApproveIn,
    ExpenseCreate,
    ExpenseOut,
    InsightOut,
    RecentOut,
    RejectIn,
    Status,
    convert_to_inr_paise,
)

router: APIRouter = APIRouter(prefix="/expenses", tags=["expenses"])

# auto_error=False so a missing header reaches require_api_key and gets our 401, rather
# than whatever status FastAPI's default error uses.
_api_key_header: APIKeyHeader = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Required for writes. Set API_KEY in .env."
)


def require_api_key(api_key: str | None = Security(_api_key_header)) -> None:
    """Allow the request only if X-API-Key matches API_KEY from the environment.

    Fails closed: if API_KEY isn't configured, every protected request is refused.
    """
    expected = os.getenv("API_KEY")
    # Compared as bytes: compare_digest raises TypeError on non-ASCII str, and Starlette
    # decodes header bytes as Latin-1, so a header like b"caf\xe9" would otherwise be a 500.
    if (
        not expected
        or api_key is None
        or not secrets.compare_digest(api_key.encode(), expected.encode())
    ):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, detail="missing or invalid X-API-Key header"
        )


MAX_INSIGHT_EXPENSES: int = 200

DEFAULT_FX_API_URL: str = "https://api.frankfurter.dev/v1/latest"
DEFAULT_FX_TIMEOUT_SECONDS: float = 3.0


class FxError(Exception):
    """The rate couldn't be fetched or wasn't usable. The message goes into fx_last_error."""


def _fetch_inr_rate(currency: str) -> Decimal:
    """Fetch how many INR one unit of `currency` is worth. Raises FxError on any FX failure."""
    url = os.getenv("FX_API_URL", DEFAULT_FX_API_URL)
    timeout = float(os.getenv("FX_TIMEOUT_SECONDS", DEFAULT_FX_TIMEOUT_SECONDS))
    try:
        response = httpx.get(
            url, params={"from": currency, "to": BASE_CURRENCY}, timeout=timeout
        )
        response.raise_for_status()
        # parse_float=Decimal: the rate never passes through a float.
        data = response.json(parse_float=Decimal)
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise FxError(f"{type(e).__name__}: {e}") from e
    except ValueError as e:  # body isn't JSON
        raise FxError(f"invalid JSON from FX API: {e}") from e

    rates = data.get("rates") if isinstance(data, dict) else None
    raw = rates.get(BASE_CURRENCY) if isinstance(rates, dict) else None
    if raw is None or isinstance(raw, bool):
        raise FxError(f"FX API response has no {BASE_CURRENCY} rate")
    try:
        rate = Decimal(str(raw))
    except InvalidOperation as e:
        raise FxError(f"non-numeric rate: {raw!r}") from e
    if not rate.is_finite() or rate <= 0:
        raise FxError(f"unusable rate: {raw!r}")
    return rate


def try_convert(expense: Expense) -> bool:
    """Fill in the INR amount if it's missing. Returns True if the expense is converted.

    Never raises for FX problems: a failure is recorded in fx_attempts / fx_last_error and
    amount_base_minor stays NULL. The caller commits.
    """
    if expense.amount_base_minor is not None:
        return True
    if expense.currency == BASE_CURRENCY:
        # INR skips the FX call entirely.
        rate = Decimal(1)
    else:
        expense.fx_attempts = (expense.fx_attempts or 0) + 1
        try:
            rate = _fetch_inr_rate(expense.currency)
        except FxError as e:
            expense.fx_last_error = str(e)
            return False

    paise = convert_to_inr_paise(expense.amount_minor, expense.currency, rate)
    if paise <= 0:
        expense.fx_last_error = f"converted amount rounds to 0 paise at rate {rate}"
        return False
    if paise > SQLITE_MAX_INT:
        # Only reachable with an absurd rate; saving it would raise OverflowError (a 500).
        expense.fx_last_error = f"converted amount too large to store at rate {rate}"
        return False
    expense.amount_base_minor = paise
    expense.fx_rate = str(rate)
    expense.fx_rate_at = utc_now_iso()
    expense.fx_last_error = None
    return True


def _get_or_404(db: Session, expense_id: int) -> Expense:
    """Load one expense or raise 404."""
    expense = db.get(Expense, expense_id)
    if expense is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="expense not found")
    return expense


def _decide(db: Session, expense_id: int, target: str, reason: str | None) -> Expense:
    """Move a pending expense to `target` (approved or rejected).

    Repeating the same decision is a no-op, changing a decision is a 409. The UPDATE is
    conditional on the row still being pending, so two concurrent approvers produce one
    change, and the loser re-reads the row and gets the same answer.
    """
    expense = _get_or_404(db, expense_id)
    if expense.status == target:
        return expense
    if expense.status != PENDING:
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"already {expense.status}")

    conditions = [Expense.id == expense_id, Expense.status == PENDING]
    if target == APPROVED:
        if expense.amount_base_minor is None:
            # Retry the conversion before refusing an unconverted expense.
            converted = try_convert(expense)
            db.commit()
            if not converted:
                raise HTTPException(
                    status.HTTP_409_CONFLICT, detail="cannot approve until converted to INR"
                )
        if expense.amount_base_minor is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT, detail="cannot approve until converted to INR"
            )
        conditions.append(Expense.amount_base_minor.is_not(None))

    db.execute(
        update(Expense)
        .where(*conditions)
        .values(status=target, decision_reason=reason, decided_at=utc_now_iso())
    )
    db.commit()
    db.refresh(expense)
    if expense.status != target:
        # Another request decided it first, the other way.
        raise HTTPException(status.HTTP_409_CONFLICT, detail=f"already {expense.status}")
    return expense


@router.post(
    "",
    response_model=ExpenseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_api_key)],
    responses={
        status.HTTP_202_ACCEPTED: {
            "model": ExpenseOut,
            "description": "Saved, but the FX API failed: amount_base_minor is null "
            "and the conversion is retried on the next GET or approve",
        }
    },
)
def create_expense(
    body: ExpenseCreate, response: Response, db: Session = Depends(get_db)
) -> Expense:
    """Submit an expense. It is saved with status `pending` and converted to INR.

    Returns 201 if the conversion worked, or 202 if the FX API failed. The expense is saved
    either way, with the original amount and currency kept.
    """
    expense = Expense(**body.model_dump(), status=PENDING, fx_attempts=0)
    # Save first, so the submission is kept even if the conversion step fails unexpectedly.
    db.add(expense)
    db.commit()

    if not try_convert(expense):
        response.status_code = status.HTTP_202_ACCEPTED
    db.commit()
    db.refresh(expense)
    return expense


@router.get("", response_model=list[ExpenseOut])
def list_expenses(
    status_filter: Status | None = Query(default=None, alias="status"),
    category: str | None = None,
    db: Session = Depends(get_db),
) -> list[Expense]:
    """List expenses, oldest first, optionally filtered by status and/or category."""
    query = select(Expense).order_by(Expense.id)
    if status_filter is not None:
        query = query.where(Expense.status == status_filter)
    if category is not None:
        query = query.where(Expense.category == category)
    return list(db.scalars(query))


# Must be registered before /{expense_id}, or "insights" is parsed as an id and 422s.
# Needs the API key: every call costs Anthropic API spend.
@router.get(
    "/insights", response_model=InsightOut, dependencies=[Depends(require_api_key)]
)
def get_insights(db: Session = Depends(get_db)) -> InsightOut:
    """Return a summary and three insights about the most recent expenses.

    Written by Claude when the Anthropic API works (`source: "ai"`), otherwise computed
    locally from the numbers (`source: "rules"`). At most MAX_INSIGHT_EXPENSES are used.
    """
    newest = db.scalars(
        select(Expense).order_by(Expense.id.desc()).limit(MAX_INSIGHT_EXPENSES)
    )
    expenses = [
        {
            "description": e.description,
            "category": e.category,
            "status": e.status,
            "amount_base_minor": e.amount_base_minor,
        }
        for e in reversed(list(newest))
    ]
    return InsightOut.model_validate({"insight": generate_insight(expenses)})


# Must also be registered before /{expense_id}.
@router.get("/recent/{n}", response_model=RecentOut)
def recent_expenses(n: int, db: Session = Depends(get_db)) -> dict:
    """Return the n most recent expenses, and the newest one's description in upper case."""
    # SQLite treats LIMIT -1 as "no limit", so clamp negatives to 0.
    newest_first = db.query(Expense).order_by(Expense.created_at.desc())
    rows = newest_first.limit(max(n, 0)).all()
    # Queried separately so "latest" doesn't depend on n (e.g. n=0 still reports it).
    newest = newest_first.first()
    # Masked before upper-casing, like every other description the API returns.
    latest = mask_pii(newest.description).upper() if newest else None
    return {"latest": latest, "items": rows}


@router.get("/{expense_id}", response_model=ExpenseOut)
def get_expense(expense_id: int, db: Session = Depends(get_db)) -> Expense:
    """Return one expense, or 404 if it doesn't exist. Retries the FX conversion if still unconverted."""
    expense = _get_or_404(db, expense_id)
    if expense.amount_base_minor is None:
        try_convert(expense)
        db.commit()
        db.refresh(expense)
    return expense


@router.post(
    "/{expense_id}/approve", response_model=ExpenseOut, dependencies=[Depends(require_api_key)]
)
def approve_expense(
    expense_id: int, body: ApproveIn | None = None, db: Session = Depends(get_db)
) -> Expense:
    """Approve a pending expense. Re-approving is a no-op; approving a rejected one is a 409."""
    reason = body.reason if body is not None else None
    return _decide(db, expense_id, APPROVED, reason)


@router.post(
    "/{expense_id}/reject", response_model=ExpenseOut, dependencies=[Depends(require_api_key)]
)
def reject_expense(expense_id: int, body: RejectIn, db: Session = Depends(get_db)) -> Expense:
    """Reject a pending expense (reason required). Re-rejecting is a no-op; rejecting an approved one is a 409."""
    return _decide(db, expense_id, REJECTED, body.reason)
