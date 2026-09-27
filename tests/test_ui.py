"""Tests for the Streamlit UI (ui/app.py), run headlessly with Streamlit's AppTest.

The API is faked by replacing httpx.request, so no server is needed. pandas is made
unimportable, as Windows Smart App Control does on the dev machine, so any UI element that
needs pandas (e.g. st.dataframe) fails here instead of in the browser.
"""

import sys
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

APP_PATH: str = str(Path(__file__).resolve().parent.parent / "ui" / "app.py")

EXPENSES: list[dict] = [
    {
        "id": 1,
        "description": "Flight | with *markdown*",
        "amount_minor": 15000,
        "currency": "USD",
        "category": "Travel",
        "submitted_by": "alice",
        "amount_base_minor": 1437300,
        "amount_base_display": "₹14,373.00",
        "status": "pending",
        "created_at": "2026-09-27T12:17:00+00:00",
    }
]


@pytest.fixture
def block_pandas(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make `import pandas` raise ImportError, like the blocked DLL does."""
    monkeypatch.setitem(sys.modules, "pandas", None)


def fake_api(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Route the UI's httpx.request calls to `handler(method, url, **kwargs)`."""
    monkeypatch.setattr(httpx, "request", handler)
    monkeypatch.setenv("API_BASE", "http://api.test")
    monkeypatch.setenv("API_KEY", "test-key")


def run_app() -> AppTest:
    at = AppTest.from_file(APP_PATH, default_timeout=30)
    at.run()
    assert not at.exception, at.exception
    return at


def test_expense_table_renders_without_pandas(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=EXPENSES)

    fake_api(monkeypatch, handler)

    at = run_app()

    table = next(m.value for m in at.markdown if m.value.startswith("| ID |"))
    assert "₹14,373\\.00" in table
    assert "150\\.00 USD" in table
    # User text is escaped, so a "|" can't add a column and "*" can't turn bold.
    assert "Flight \\| with \\*markdown\\*" in table


def test_api_down_shows_friendly_error(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake_api(monkeypatch, handler)

    at = run_app()

    assert any("Can't reach the ExpenseFlow API" in e.value for e in at.error)


def test_table_shows_rupees_and_status_labels(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    expenses = [
        {**EXPENSES[0], "id": 1, "status": "pending", "amount_base_minor": 1_25_00_000},
        {**EXPENSES[0], "id": 2, "status": "approved", "amount_base_minor": 50},
        {**EXPENSES[0], "id": 3, "status": "rejected", "amount_base_minor": None},
    ]

    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        return httpx.Response(200, json=expenses)

    fake_api(monkeypatch, handler)

    table = next(m.value for m in run_app().markdown if m.value.startswith("| ID |"))

    # Formatted in the UI from integer paise: two decimals, lakh/crore grouping.
    assert "₹1,25,000\.00" in table
    assert "₹0\.50" in table
    assert "awaiting conversion" in table
    assert "🟡 Pending" in table and "🟢 Approved" in table and "🔴 Rejected" in table


def fill_and_submit(at: AppTest, amount: str = "45.99", currency: str = "EUR") -> None:
    at.text_input(key="amount").input(amount)
    at.text_input(key="currency").input(currency)
    at.text_input(key="category").input("Software")
    at.text_input(key="description").input("IDE licence")
    at.text_input(key="submitted_by").input("carol")
    next(b for b in at.button if b.label == "Submit expense").click()
    at.run()
    assert not at.exception, at.exception


def submit_button(at: AppTest):
    return next(b for b in at.button if b.label == "Submit expense")


def test_submit_button_disabled_while_request_in_flight(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    import streamlit as st

    seen_during_post: list[bool] = []

    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        if method == "POST":
            # Runs mid-script, while the request is "in flight".
            seen_during_post.append(st.session_state["submitting"])
            return httpx.Response(
                201, json={**EXPENSES[0], "id": 7, "amount_base_minor": 440354}
            )
        return httpx.Response(200, json=[])

    fake_api(monkeypatch, handler)
    at = run_app()
    assert submit_button(at).disabled is False

    fill_and_submit(at)

    assert seen_during_post == [True]
    # After the request, the rerun shows the result and the button is usable again.
    assert [s.value for s in at.success] == ["Saved expense #7: 45.99 EUR = ₹4,403.54."]
    assert submit_button(at).disabled is False


def test_submit_button_reenabled_after_validation_error(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        assert method != "POST", "invalid input must not be sent"
        return httpx.Response(200, json=[])

    fake_api(monkeypatch, handler)
    at = run_app()

    fill_and_submit(at, amount="12.345", currency="USD")

    assert [e.value for e in at.error] == ["USD amounts can have at most 2 decimal places."]
    assert submit_button(at).disabled is False


def test_submit_button_reenabled_when_api_is_down(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    fake_api(monkeypatch, handler)
    at = run_app()

    fill_and_submit(at)

    assert any("Can't reach the ExpenseFlow API" in e.value for e in at.error)
    assert submit_button(at).disabled is False


def chart_expense(id_: int, category: str, paise: int | None, created: str) -> dict:
    return {
        **EXPENSES[0],
        "id": id_,
        "description": f"expense {id_}",
        "category": category,
        "amount_base_minor": paise,
        "created_at": f"{created}T10:00:00+00:00",
    }


def test_insights_show_spend_chart_by_category(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    import json

    expenses = [
        chart_expense(1, "Travel", 1437300, "2026-09-20"),
        chart_expense(2, "Travel", 50000, "2026-09-21"),
        chart_expense(3, "Meals", 125050, "2026-09-22"),
        chart_expense(4, "Meals", None, "2026-09-23"),  # unconverted: not charted
    ]

    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/insights"):
            insight = {"summary": "s", "bullets": ["a", "b", "c"], "source": "rules"}
            return httpx.Response(200, json={"insight": insight})
        return httpx.Response(200, json=expenses)

    fake_api(monkeypatch, handler)
    at = run_app()
    assert not at.get("vega_lite_chart")  # only after clicking the button

    next(b for b in at.button if b.label == "Generate insights").click()
    at.run()
    assert not at.exception, at.exception

    chart = at.get("vega_lite_chart")[0]
    spec = json.loads(chart.proto.spec)
    # Data stays inside the layers, so Streamlit never converts it with pandas.
    assert "data" not in spec and not chart.proto.data.data
    segments, _, totals = (layer["data"]["values"] for layer in spec["layer"])

    # Categories ordered by total, labelled exactly from integer paise.
    assert [(t["category"], t["total_label"]) for t in totals] == [
        ("Travel", "₹14,873.00"),
        ("Meals", "₹1,250.50"),
    ]
    # One segment per converted expense, oldest at the bottom, each with its date for hover.
    travel = [s for s in segments if s["category"] == "Travel"]
    assert [(s["date"], s["amount_label"], s["y0"], s["y1"], s["is_top"]) for s in travel] == [
        ("2026-09-20", "₹14,373.00", 0.0, 14373.0, False),
        ("2026-09-21", "₹500.00", 14373.0, 14873.0, True),
    ]
    assert "expense 4" not in json.dumps(spec)
    tooltip_titles = [t["title"] for t in spec["layer"][0]["encoding"]["tooltip"]]
    assert tooltip_titles[:2] == ["Amount", "Date (UTC)"]
    assert "1 expense(s) awaiting conversion" in at.caption[-1].value


def test_spend_chart_when_nothing_is_converted(
    monkeypatch: pytest.MonkeyPatch, block_pandas: None
) -> None:
    def handler(method: str, url: str, **kwargs: object) -> httpx.Response:
        if url.endswith("/insights"):
            insight = {"summary": "s", "bullets": [], "source": "rules"}
            return httpx.Response(200, json={"insight": insight})
        return httpx.Response(200, json=[chart_expense(1, "Travel", None, "2026-09-20")])

    fake_api(monkeypatch, handler)
    at = run_app()
    next(b for b in at.button if b.label == "Generate insights").click()
    at.run()

    assert not at.exception, at.exception
    assert not at.get("vega_lite_chart")
    assert any("nothing to chart" in i.value for i in at.info)
