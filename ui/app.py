"""Streamlit front end for the ExpenseFlow API: submit expenses, list them, and show insights.

It only talks to the API over HTTP (httpx). The base URL comes from API_BASE (default
http://127.0.0.1:8000) and the X-API-Key from API_KEY, both read from the environment or
the project's .env.

Run from the project root, with the API running:
    streamlit run ui/app.py
"""

import os
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import streamlit as st
from dotenv import load_dotenv

PROJECT_ROOT: Path = Path(__file__).resolve().parent.parent
# Reuse the API's own currency rules rather than a copy that could drift.
sys.path.insert(0, str(PROJECT_ROOT))
from app.schemas import MAX_AMOUNT_MINOR, currency_exponent, format_inr  # noqa: E402

load_dotenv(PROJECT_ROOT / ".env")

API_BASE: str = os.getenv("API_BASE", "http://127.0.0.1:8000").rstrip("/")
API_KEY: str = os.getenv("API_KEY", "")
TIMEOUT_SECONDS: float = 15.0  # submit may wait on the FX API, insights on the Anthropic API

# Characters that Streamlit markdown would interpret ($ starts LaTeX).
_MARKDOWN_SPECIAL: str = "\\`*_{}[]()#+-.!|<>$~"

# Status shown with a colour marker plus the word, so it doesn't rely on colour alone.
STATUS_LABELS: dict[str, str] = {
    "pending": "🟡 Pending",
    "approved": "🟢 Approved",
    "rejected": "🔴 Rejected",
}

# Spend chart colours, per Streamlit theme. One series, so one hue (validated >= 3:1 against
# each surface); text and gridlines use text/neutral tones, never the bar colour.
CHART_THEMES: dict[str, dict[str, str]] = {
    "light": {
        "bar": "#2a78d6",
        "surface": "#ffffff",
        "text": "#31333f",
        "muted": "#6b6c75",
        "grid": "#ebebeb",
    },
    "dark": {
        "bar": "#3987e5",
        "surface": "#0e1117",
        "text": "#fafafa",
        "muted": "#a3a8b8",
        "grid": "#262730",
    },
}
# Vega expression: y-axis ticks as ₹ with Indian digit grouping (₹1,25,000).
INR_AXIS_LABEL: str = (
    r"'₹' + (datum.value < 1000 ? format(datum.value, 'd') : "
    r"replace(slice(format(datum.value, 'd'), 0, -3), regexp('\\B(?=(\\d{2})+(?!\\d))', 'g'), ',')"
    r" + ',' + slice(format(datum.value, 'd'), -3))"
)

# Session-state keys for the submit flow.
SUBMITTING: str = "submitting"  # True while a POST is in flight; disables the button
PENDING_SUBMISSION: str = "pending_submission"  # form values captured by the click callback
SUBMIT_RESULT: str = "submit_result"  # (kind, message) shown after the rerun
FORM_FIELDS: tuple[str, ...] = ("amount", "currency", "category", "description", "submitted_by")


class ApiError(Exception):
    """A problem talking to the API, with a message fit to show the user."""


def api_request(method: str, path: str, **kwargs: object) -> httpx.Response:
    """Send a request to the API. Connection problems become an ApiError, never a traceback."""
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    try:
        return httpx.request(
            method, f"{API_BASE}{path}", headers=headers, timeout=TIMEOUT_SECONDS, **kwargs
        )
    except httpx.ConnectError:
        raise ApiError(
            f"Can't reach the ExpenseFlow API at {API_BASE}. Is it running? Start it from "
            "the project folder with: python -m uvicorn app.main:app --reload"
        ) from None
    except httpx.TimeoutException:
        raise ApiError(
            f"The API at {API_BASE} took more than {TIMEOUT_SECONDS:.0f} seconds to respond. "
            "Please try again."
        ) from None
    except (httpx.HTTPError, httpx.InvalidURL) as e:
        raise ApiError(f"Couldn't talk to the API at {API_BASE}: {e}") from None


def error_message(response: httpx.Response) -> str:
    """A readable explanation of an error response."""
    if response.status_code == 401:
        return (
            "The API refused the request: the API key is missing or wrong. Set API_KEY in "
            ".env to the same value the API server uses, then restart this app."
        )
    try:
        detail = response.json().get("detail")
    except ValueError:
        detail = response.text
    if response.status_code == 422 and isinstance(detail, list):
        problems = "; ".join(
            f"{'.'.join(str(p) for p in d.get('loc', [])[1:])}: {d.get('msg')}" for d in detail
        )
        return f"The API rejected the expense: {problems}"
    return f"The API returned an error ({response.status_code}): {detail}"


def to_minor_units(amount_text: str, currency: str) -> int:
    """Turn '1250.50' into minor units (125050) using the currency's decimal places."""
    try:
        amount = Decimal(amount_text.strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError(f"'{amount_text}' isn't a number.") from None
    if not amount.is_finite() or amount <= 0:
        raise ValueError("The amount must be greater than zero.")
    places = currency_exponent(currency)
    minor = amount.scaleb(places)
    if minor != minor.to_integral_value():
        raise ValueError(f"{currency} amounts can have at most {places} decimal places.")
    if minor > MAX_AMOUNT_MINOR:
        raise ValueError("That amount is too large.")
    return int(minor)


def format_original(amount_minor: int, currency: str) -> str:
    """Minor units back to a readable amount, e.g. 125050 USD -> '1250.50 USD'."""
    places = currency_exponent(currency)
    return f"{Decimal(amount_minor).scaleb(-places):.{places}f} {currency}"


def format_rupees(paise: int | None) -> str:
    """INR paise as rupees with two decimals and lakh/crore grouping, for display only.

    e.g. 12500000 -> '₹1,25,000.00'. None means the expense isn't converted to INR yet.
    """
    return "awaiting conversion" if paise is None else format_inr(paise)


def status_label(status: str) -> str:
    """'approved' -> '🟢 Approved'; unknown values are shown as-is, capitalised."""
    return STATUS_LABELS.get(status, status.capitalize())


def escape_markdown(text: str) -> str:
    """Show user-derived text literally in st.markdown."""
    return "".join(f"\\{c}" if c in _MARKDOWN_SPECIAL else c for c in text)


def _start_submit() -> None:
    """Submit-button callback: capture the form values and mark a request as in flight.

    Callbacks run before the script, so the run that follows draws the button disabled.
    The values have to be captured here: Streamlit discards the click value of a widget
    that is registered as disabled in the same run.
    """
    st.session_state[PENDING_SUBMISSION] = {
        field: str(st.session_state.get(field, "")) for field in FORM_FIELDS
    }
    st.session_state[SUBMITTING] = True


def submit_section() -> None:
    """Form that POSTs a new expense. The button is disabled while the request is in flight."""
    st.subheader("Submit an expense")
    with st.form("new_expense", clear_on_submit=True):
        left, right = st.columns([3, 1])
        left.text_input("Amount", placeholder="e.g. 1250.50", key="amount")
        right.text_input("Currency", value="INR", max_chars=3, key="currency")
        st.text_input("Category", placeholder="e.g. travel", key="category")
        st.text_input("Description", key="description")
        st.text_input("Submitted by", key="submitted_by")
        st.form_submit_button(
            "Submit expense",
            key="submit_expense",
            disabled=st.session_state.get(SUBMITTING, False),
            on_click=_start_submit,
        )

    # Result of the previous submission, stored before the rerun that re-enabled the button.
    result = st.session_state.pop(SUBMIT_RESULT, None)
    if result is not None:
        kind, message = result
        getattr(st, kind)(message)

    # Taken out before sending, so an interrupted run can never submit the same values twice.
    pending = st.session_state.pop(PENDING_SUBMISSION, None)
    if pending is None:
        st.session_state[SUBMITTING] = False  # clears a flag left by an interrupted run
        return
    try:
        with st.spinner("Saving expense..."):
            st.session_state[SUBMIT_RESULT] = submit_expense(
                pending["amount"],
                pending["currency"],
                pending["category"],
                pending["description"],
                pending["submitted_by"],
            )
    finally:
        st.session_state[SUBMITTING] = False
    st.rerun()  # redraw with the button enabled and the result shown


def submit_expense(
    amount_text: str, currency: str, category: str, description: str, submitted_by: str
) -> tuple[str, str]:
    """Validate the form and POST it. Returns (st method name, message) to show the user."""
    currency = currency.strip().upper()
    missing = [
        name
        for name, value in [
            ("amount", amount_text),
            ("currency", currency),
            ("category", category),
            ("description", description),
            ("submitted by", submitted_by),
        ]
        if not value.strip()
    ]
    if missing:
        return "error", f"Please fill in: {', '.join(missing)}."
    try:
        amount_minor = to_minor_units(amount_text, currency)
    except ValueError as e:
        return "error", str(e)

    payload = {
        "description": description.strip(),
        "amount_minor": amount_minor,
        "currency": currency,
        "category": category.strip(),
        "submitted_by": submitted_by.strip(),
    }
    try:
        response = api_request("POST", "/expenses", json=payload)
    except ApiError as e:
        return "error", str(e)

    if response.status_code == 201:
        body = response.json()
        return "success", (
            f"Saved expense #{body['id']}: {format_original(amount_minor, currency)} "
            f"= {format_rupees(body['amount_base_minor'])}."
        )
    if response.status_code == 202:
        body = response.json()
        return "warning", (
            f"Saved expense #{body['id']}, but it couldn't be converted to INR yet (the "
            "exchange-rate service is unavailable). It will be retried automatically."
        )
    return "error", error_message(response)


def expenses_section() -> None:
    """Table of existing expenses, from GET /expenses."""
    header, refresh = st.columns([4, 1])
    header.subheader("Expenses")
    refresh.button("Refresh", key="refresh")  # any click reruns the script and refetches

    try:
        response = api_request("GET", "/expenses")
    except ApiError as e:
        st.error(str(e))
        return
    if response.status_code != 200:
        st.error(error_message(response))
        return

    expenses = response.json()
    if not expenses:
        st.info("No expenses yet. Submit one above.")
        return
    st.markdown(expenses_markdown_table(expenses))


def expenses_markdown_table(expenses: list[dict]) -> str:
    """The expenses as a Markdown table.

    Not st.dataframe: that imports pandas, whose compiled extension Windows Smart App
    Control blocks on this machine. Every cell is escaped, so user text can't break the table.
    """
    columns: list[tuple[str, str]] = [  # (header, alignment row)
        ("ID", "---:"),
        ("Description", "---"),
        ("Category", "---"),
        ("Original amount", "---:"),
        ("Amount (₹)", "---:"),
        ("Status", "---"),
        ("Submitted by", "---"),
        ("Created (UTC)", "---"),
    ]
    lines = [
        "| " + " | ".join(header for header, _ in columns) + " |",
        "|" + "|".join(align for _, align in columns) + "|",
    ]
    for e in expenses:
        cells = [
            str(e["id"]),
            e["description"],
            e["category"],
            format_original(e["amount_minor"], e["currency"]),
            format_rupees(e["amount_base_minor"]),
            status_label(e["status"]),
            e["submitted_by"],
            e["created_at"][:16].replace("T", " "),
        ]
        lines.append(
            "| " + " | ".join(escape_markdown(" ".join(c.split())) for c in cells) + " |"
        )
    return "\n".join(lines)


def insights_section() -> None:
    """Button that fetches GET /expenses/insights and shows the summary and bullets."""
    st.subheader("Insights")
    if not st.button("Generate insights", key="generate_insights"):
        return
    try:
        with st.spinner("Generating insights..."):
            response = api_request("GET", "/expenses/insights")
    except ApiError as e:
        st.error(str(e))
        return
    if response.status_code != 200:
        st.error(error_message(response))
        return

    insight = response.json()["insight"]
    st.markdown(f"**{escape_markdown(insight['summary'])}**")
    for bullet in insight["bullets"]:
        st.markdown(f"- {escape_markdown(bullet)}")
    if insight.get("source") == "ai":
        st.caption("Written by Claude.")
    else:
        st.caption("Computed from your expense data (Claude was not used).")

    spend_chart()


def spend_chart_data(expenses: list[dict]) -> tuple[list[dict], list[dict]]:
    """Per-expense bar segments and per-category totals for the spend chart.

    Only converted expenses (with an INR amount) are included. Within a category, segments
    are stacked oldest first; categories are ordered by total, largest first. Plot positions
    are rupees as floats (display only); every value a person reads comes from the exact
    integer paise via format_rupees.
    """
    by_category: dict[str, list[dict]] = {}
    for e in sorted(expenses, key=lambda e: (e["created_at"], e["id"])):
        if e["amount_base_minor"] is not None:
            by_category.setdefault(e["category"], []).append(e)

    segments: list[dict] = []
    totals: list[dict] = []
    for category, items in by_category.items():
        total_paise = sum(e["amount_base_minor"] for e in items)
        running = 0
        for i, e in enumerate(items):
            segments.append(
                {
                    "category": category,
                    "date": e["created_at"][:10],
                    "description": e["description"],
                    "amount_label": format_rupees(e["amount_base_minor"]),
                    "total_label": format_rupees(total_paise),
                    "y0": running / 100,
                    "y1": (running + e["amount_base_minor"]) / 100,
                    "is_top": i == len(items) - 1,
                }
            )
            running += e["amount_base_minor"]
        totals.append(
            {
                "category": category,
                "total": total_paise / 100,
                "total_label": format_rupees(total_paise),
            }
        )
    totals.sort(key=lambda t: -t["total"])
    return segments, totals


def spend_chart_spec(segments: list[dict], totals: list[dict], mode: str) -> dict:
    """Vega-Lite spec: one bar per category, one segment per expense (hover shows its date).

    Data sits inside each layer, not at the top level: Streamlit converts top-level data with
    pandas, which Smart App Control blocks here. Inline layer data goes straight to the browser.
    """
    colors = CHART_THEMES[mode]
    order = [t["category"] for t in totals]
    bar = {"type": "bar", "width": 24, "stroke": colors["surface"], "strokeWidth": 2}
    tooltip = [
        {"field": "amount_label", "title": "Amount"},
        {"field": "date", "title": "Date (UTC)"},
        {"field": "description", "title": "Expense"},
        {"field": "category", "title": "Category"},
        {"field": "total_label", "title": "Category total"},
    ]

    def segment_layer(top: bool) -> dict:
        name = "hover_top" if top else "hover_rest"
        mark = {**bar, "cornerRadiusTopLeft": 4, "cornerRadiusTopRight": 4} if top else bar
        return {
            "data": {"values": segments},
            "transform": [{"filter": "datum.is_top" if top else "!datum.is_top"}],
            "params": [
                {"name": name, "select": {"type": "point", "on": "pointerover", "clear": "pointerout"}}
            ],
            "mark": mark,
            "encoding": {
                "x": {"field": "category", "type": "nominal", "sort": order, "title": None,
                      "axis": {"labelAngle": 0}},
                "y": {"field": "y1", "type": "quantitative", "title": "Spend (₹)",
                      "axis": {"labelExpr": INR_AXIS_LABEL}},
                "y2": {"field": "y0"},
                "color": {"value": colors["bar"]},
                # The hovered segment lightens so the reader sees it respond.
                "opacity": {"condition": {"param": name, "empty": False, "value": 0.7}, "value": 1},
                "tooltip": tooltip,
            },
        }

    return {
        "height": 320,
        "padding": {"top": 18, "right": 5, "bottom": 5, "left": 5},  # room for the tallest total
        "layer": [
            segment_layer(top=False),
            segment_layer(top=True),
            {
                # Category total on each bar's cap, in text colour (never the bar colour).
                "data": {"values": totals},
                "mark": {"type": "text", "dy": -8, "fontSize": 12, "color": colors["text"]},
                "encoding": {
                    "x": {"field": "category", "type": "nominal", "sort": order},
                    "y": {"field": "total", "type": "quantitative"},
                    "text": {"field": "total_label"},
                },
            },
        ],
        "config": {
            "view": {"stroke": None},
            "axis": {
                "gridColor": colors["grid"],
                "domainColor": colors["grid"],
                "tickColor": colors["grid"],
                "labelColor": colors["muted"],
                "titleColor": colors["muted"],
            },
        },
    }


def spend_chart() -> None:
    """Bar chart of INR spend by category, from GET /expenses."""
    st.markdown("**Spend by category**")
    try:
        response = api_request("GET", "/expenses")
    except ApiError as e:
        st.error(str(e))
        return
    if response.status_code != 200:
        st.error(error_message(response))
        return

    expenses = response.json()
    segments, totals = spend_chart_data(expenses)
    if not segments:
        st.info("No expenses have been converted to INR yet, so there's nothing to chart.")
        return
    mode = "dark" if st.context.theme.type == "dark" else "light"
    st.vega_lite_chart(spend_chart_spec(segments, totals, mode), width="stretch")
    unconverted = sum(1 for e in expenses if e["amount_base_minor"] is None)
    note = "Each bar is one category; each segment is one expense. Hover a segment for its date."
    if unconverted:
        note += f" {unconverted} expense(s) awaiting conversion to INR aren't shown."
    st.caption(note)


st.set_page_config(page_title="ExpenseFlow", page_icon="💸")
st.title("💸 ExpenseFlow")
st.caption(
    "Submit expenses in any currency, see them converted to rupees, track their approval "
    f"status and get quick spending insights. Connected to {API_BASE}."
)
if not API_KEY:
    st.warning(
        "API_KEY isn't set, so submitting expenses and generating insights will be refused. "
        "Add it to .env."
    )

submit_section()
st.divider()
expenses_section()  # after the form, so a just-submitted expense shows up in this run
st.divider()
insights_section()
