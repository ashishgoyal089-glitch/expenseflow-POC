"""SQLAlchemy ORM model for the expenses table."""

from datetime import UTC, datetime

from sqlalchemy import CheckConstraint, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

PENDING: str = "pending"
APPROVED: str = "approved"
REJECTED: str = "rejected"
STATUSES: tuple[str, ...] = (PENDING, APPROVED, REJECTED)
FINAL_STATUSES: tuple[str, ...] = (APPROVED, REJECTED)


def utc_now_iso() -> str:
    """Current UTC time as an ISO 8601 string, e.g. 2026-09-26T10:15:30.123456+00:00."""
    return datetime.now(UTC).isoformat()


class Expense(Base):
    """One submitted expense. All amounts are integer minor units, never floats."""

    __tablename__ = "expenses"
    __table_args__ = (
        CheckConstraint("amount_minor > 0", name="ck_expenses_amount_positive"),
        CheckConstraint(
            "amount_base_minor IS NULL OR amount_base_minor > 0",
            name="ck_expenses_base_positive",
        ),
        CheckConstraint(
            "status IN ('pending', 'approved', 'rejected')", name="ck_expenses_status"
        ),
        # A pending expense may still be unconverted (FX API was down), but nothing can
        # be approved without an INR amount. Rejecting doesn't need one.
        CheckConstraint(
            "status != 'approved' OR amount_base_minor IS NOT NULL",
            name="ck_expenses_approved_has_base",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    submitted_by: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(Text, nullable=False)
    # Amount as submitted, in minor units of currency (cents, paise, ...).
    amount_minor: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False)
    # Amount in INR paise. NULL until the FX conversion succeeds.
    amount_base_minor: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Rate as a decimal string (not float) so the conversion can be reproduced exactly.
    fx_rate: Mapped[str | None] = mapped_column(Text, nullable=True)
    fx_rate_at: Mapped[str | None] = mapped_column(Text, nullable=True)
    fx_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    fx_last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=PENDING, server_default=PENDING
    )
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[str] = mapped_column(Text, nullable=False, default=utc_now_iso)
    decided_at: Mapped[str | None] = mapped_column(Text, nullable=True)
