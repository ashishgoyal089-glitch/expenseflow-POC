"""Pydantic v2 request and response models, plus the money helpers.

All amounts are integer minor units, never floats. Conversion uses Decimal and rounds
half-up once, to whole paise.
"""

from datetime import datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator

from app.sanitize import MASKED_FIELDS, mask_pii

Status = Literal["pending", "approved", "rejected"]

BASE_CURRENCY: str = "INR"

# Largest integer SQLite can store; anything bigger raises OverflowError on write.
SQLITE_MAX_INT: int = 2**63 - 1
# Input cap on amount_minor: 10^12 minor units (e.g. ₹1,000 crore). Far above any real
# expense, and small enough that converting at any plausible rate stays storable.
MAX_AMOUNT_MINOR: int = 10**12

# ISO 4217 minor-unit exponents that differ from the usual 2.
_CURRENCY_EXPONENTS: dict[str, int] = {
    "BIF": 0, "CLP": 0, "DJF": 0, "GNF": 0, "ISK": 0, "JPY": 0, "KMF": 0, "KRW": 0,
    "PYG": 0, "RWF": 0, "UGX": 0, "VND": 0, "VUV": 0, "XAF": 0, "XOF": 0, "XPF": 0,
    "BHD": 3, "IQD": 3, "JOD": 3, "KWD": 3, "LYD": 3, "OMR": 3, "TND": 3,
}


def currency_exponent(currency: str) -> int:
    """Number of minor-unit digits for `currency`, e.g. JPY 0, USD 2, KWD 3."""
    return _CURRENCY_EXPONENTS.get(currency, 2)


def convert_to_inr_paise(amount_minor: int, currency: str, rate: Decimal) -> int:
    """Convert minor units of `currency` to INR paise at `rate` (INR per 1 unit), half-up once."""
    major = Decimal(amount_minor).scaleb(-currency_exponent(currency))
    paise = major * rate * 100
    # to_integral_value, not quantize: quantize raises InvalidOperation when the result has
    # more digits than the context precision (28), turning an absurd rate into a 500.
    # Callers range-check the result instead.
    return int(paise.to_integral_value(rounding=ROUND_HALF_UP))


def format_inr(paise: int) -> str:
    """Format INR paise with the ₹ symbol and lakh/crore grouping, e.g. 12500000 -> ₹1,25,000.00."""
    sign = "-" if paise < 0 else ""
    rupees, frac = divmod(abs(paise), 100)
    digits = str(rupees)
    if len(digits) > 3:
        head, tail = digits[:-3], digits[-3:]
        groups = []
        while len(head) > 2:
            groups.insert(0, head[-2:])
            head = head[:-2]
        if head:
            groups.insert(0, head)
        digits = ",".join(groups) + "," + tail
    return f"{sign}₹{digits}.{frac:02d}"


class ExpenseCreate(BaseModel):
    """Request body for submitting an expense."""

    model_config = ConfigDict(str_strip_whitespace=True)

    description: str = Field(min_length=1)
    # Minor units of `currency` (cents, paise, ...). strict=True rejects floats and numeric strings.
    amount_minor: int = Field(gt=0, le=MAX_AMOUNT_MINOR, strict=True)
    # ISO 4217 code. Lowercase input is accepted and uppercased before the pattern check.
    currency: str = Field(pattern=r"^[A-Z]{3}$")
    category: str = Field(min_length=1)
    submitted_by: str = Field(min_length=1)

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: object) -> object:
        """Uppercase the currency code so "usd" and "USD" are treated the same."""
        return value.upper() if isinstance(value, str) else value


class ApproveIn(BaseModel):
    """Request body for approving an expense. The reason is optional."""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str | None = None


class RejectIn(BaseModel):
    """Request body for rejecting an expense. A reason is required."""

    model_config = ConfigDict(str_strip_whitespace=True)

    reason: str = Field(min_length=1)


class ExpenseOut(BaseModel):
    """An expense as returned by the API.

    Free-text fields (description, category) have emails, phone numbers and card/account
    numbers masked. The database keeps the original text.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    description: str
    amount_minor: int
    currency: str
    category: str
    submitted_by: str
    # INR paise. None while the FX conversion hasn't succeeded yet.
    amount_base_minor: int | None
    fx_rate: str | None
    fx_rate_at: datetime | None
    fx_attempts: int
    fx_last_error: str | None
    status: Status
    decision_reason: str | None
    created_at: datetime
    decided_at: datetime | None

    @field_validator(*MASKED_FIELDS)
    @classmethod
    def mask_personal_data(cls, value: str) -> str:
        """Mask PII in free text before it leaves the API."""
        return mask_pii(value)

    @computed_field
    @property
    def amount_base_display(self) -> str | None:
        """The INR amount formatted like ₹1,25,000.00, or None while unconverted."""
        return None if self.amount_base_minor is None else format_inr(self.amount_base_minor)


class RecentOut(BaseModel):
    """The n most recent expenses, plus the newest expense's description in upper case."""

    latest: str | None
    items: list[ExpenseOut]


class InsightBody(BaseModel):
    """A one-sentence summary plus three bullet insights (bullets is empty if no expenses).

    `source` is "ai" when Claude wrote it, or "rules" when it was computed locally because
    the Anthropic API was unavailable or INSIGHTS_PROVIDER=rules.
    """

    summary: str
    bullets: list[str]
    source: Literal["ai", "rules"]


class InsightOut(BaseModel):
    """AI-generated spending insights, or a fallback message if they couldn't be generated."""

    insight: InsightBody
