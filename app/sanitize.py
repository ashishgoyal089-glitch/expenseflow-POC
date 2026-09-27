"""Mask personal data (emails, phone numbers, card/account numbers) in free-text fields.

Used before user-supplied text leaves the app, e.g. before expense descriptions are sent to
the Anthropic API. Each match is replaced by a typed placeholder: [EMAIL], [PHONE], [CARD].

Digit runs are classified by how many digits they contain, ignoring single space, hyphen or
dot separators:
    - starts with "+" (country code), 8-15 digits  -> [PHONE]   e.g. +91 98765 43210
    - 10-11 digits                                 -> [PHONE]   e.g. 98765-43210, (022) 2345 6789
    - 12 or more digits                            -> [CARD]    e.g. 4111 1111 1111 1111, account numbers
Shorter runs (amounts, dates, invoice numbers) are left alone. A run is still found when it
touches letters or underscores (card_4111..., tel9876543210). Only free-text fields are
masked; structured numeric fields such as amount_base_minor are never touched.
"""

import re

EMAIL_PLACEHOLDER: str = "[EMAIL]"
PHONE_PLACEHOLDER: str = "[PHONE]"
CARD_PLACEHOLDER: str = "[CARD]"

# User-supplied free-text fields that mask_expense() masks.
MASKED_FIELDS: tuple[str, ...] = ("description", "category")

_EMAIL_RE: re.Pattern[str] = re.compile(
    r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b"
)

# An optional "+" and/or "(area code)", then digits separated by at most one space, hyphen
# or dot. The lookarounds only stop a match from starting or ending inside a number; letters
# and underscores next to it don't matter, so "card_4111..." is still caught.
_DIGIT_RUN_RE: re.Pattern[str] = re.compile(
    r"(?<![\d+])\+?(?:\(\d{1,5}\)[ .-]?)?\d(?:[ .-]?\d)*(?!\d)"
)
_NON_DIGIT_RE: re.Pattern[str] = re.compile(r"\D")


def _classify_digit_run(match: re.Match[str]) -> str:
    """Replace a digit run with [PHONE] or [CARD], or keep it if it's too short to be either."""
    run = match.group(0)
    digits = len(_NON_DIGIT_RE.sub("", run))
    if run.startswith("+"):
        return PHONE_PLACEHOLDER if 8 <= digits <= 15 else run
    if digits >= 12:
        return CARD_PLACEHOLDER
    if digits >= 10:
        return PHONE_PLACEHOLDER
    return run


def mask_pii(text: str) -> str:
    """Return `text` with emails, phone numbers and card/account numbers replaced by placeholders."""
    text = _EMAIL_RE.sub(EMAIL_PLACEHOLDER, text)
    return _DIGIT_RUN_RE.sub(_classify_digit_run, text)


def mask_expense(expense: dict) -> dict:
    """Return a shallow copy of `expense` with its free-text fields (MASKED_FIELDS) masked.

    Other fields, including numeric ones like amount_base_minor, are unchanged.
    """
    masked = dict(expense)
    for field in MASKED_FIELDS:
        value = masked.get(field)
        if isinstance(value, str):
            masked[field] = mask_pii(value)
    return masked
