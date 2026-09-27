"""Tests for PII masking in app/sanitize.py."""

import pytest

from app.sanitize import mask_expense, mask_pii


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("mail alice.smith+x@corp.example.co.in now", "mail [EMAIL] now"),
        ("call +91 98765 43210", "call [PHONE]"),
        ("call 98765-43210 or (022) 2345 6789", "call [PHONE] or [PHONE]"),
        ("card 4111 1111 1111 1111", "card [CARD]"),
        ("account 123456789012345", "account [CARD]"),
        # Found even when touching letters or underscores (code review finding).
        ("card_4111111111111111", "card_[CARD]"),
        ("tel9876543210", "tel[PHONE]"),
        ("4111111111111111x", "[CARD]x"),
        ("id john1234567890123@example.com", "id [EMAIL]"),
    ],
)
def test_masks_pii(text: str, expected: str) -> None:
    assert mask_pii(text) == expected


@pytest.mark.parametrize(
    "text",
    [
        "Taxi 450.50 on 2026-09-26",
        "invoice 20260926, flight 6E 2345",
        "₹1,25,000.00",
        "Order 12345 x 3, room 402, year 2026",
        "short +44 12",
    ],
)
def test_leaves_short_numbers_alone(text: str) -> None:
    assert mask_pii(text) == text


def test_mask_expense_masks_free_text_fields_only() -> None:
    expense = {
        "description": "dinner, card 4111 1111 1111 1111",
        "category": "refund to bob@example.com",
        "amount_base_minor": 1234567890123,
        "submitted_by": "alice",
    }

    masked = mask_expense(expense)

    assert masked == {
        "description": "dinner, card [CARD]",
        "category": "refund to [EMAIL]",
        "amount_base_minor": 1234567890123,
        "submitted_by": "alice",
    }
    assert expense["description"].endswith("1111")  # original not modified
