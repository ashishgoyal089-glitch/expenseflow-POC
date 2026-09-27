"""API tests for the expense endpoints, run with FastAPI's TestClient against app.main:app.

Each test gets a fresh SQLite database in pytest's tmp_path, wired in by overriding the
get_db dependency, so tests never touch expenseflow.db or depend on each other. API_KEY is
set to a test value, and the `client` fixture sends it in X-API-Key by default.
"""

from collections.abc import Iterator

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

import app.models  # noqa: F401  (registers the Expense table on Base.metadata)
from app.db import Base, get_db
from app.main import app
from app.models import Expense

EXPENSE: dict[str, object] = {
    "description": "Flight to Bangalore",
    "amount_minor": 1_25_050,
    "currency": "INR",
    "category": "travel",
    "submitted_by": "alice",
}

TEST_API_KEY: str = "test-api-key"


@pytest.fixture
def client(tmp_path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A TestClient with a valid X-API-Key whose requests use a new, empty SQLite database."""
    # Overrides any API_KEY loaded from the real .env, for this test only.
    monkeypatch.setenv("API_KEY", TEST_API_KEY)
    engine = create_engine(
        f"sqlite:///{tmp_path / 'test.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(bind=engine)
    testing_session = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def override_get_db() -> Iterator[Session]:
        db = testing_session()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_get_db
    # Not used as a context manager on purpose: that would run the app's lifespan, whose
    # init_db() targets the real expenseflow.db engine.
    yield TestClient(app, headers={"X-API-Key": TEST_API_KEY})
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture
def anon_client(client: TestClient) -> TestClient:
    """Same app and test database as `client`, but sends no X-API-Key header."""
    return TestClient(app)


def create(client: TestClient, **overrides: object) -> dict:
    """POST an expense (EXPENSE plus any overrides) and return the response body."""
    response = client.post("/expenses", json={**EXPENSE, **overrides})
    assert response.status_code == 201, response.text
    return response.json()


def test_create_expense_returns_pending_with_id(client: TestClient) -> None:
    body = create(client)

    assert isinstance(body["id"], int)
    assert body["status"] == "pending"
    assert body["description"] == EXPENSE["description"]
    assert body["amount_minor"] == EXPENSE["amount_minor"]


def test_list_filters_by_status(client: TestClient) -> None:
    pending = create(client, description="Taxi")
    approved = create(client, description="Hotel")
    assert client.post(f"/expenses/{approved['id']}/approve").status_code == 200

    pending_ids = [e["id"] for e in client.get("/expenses", params={"status": "pending"}).json()]
    approved_ids = [e["id"] for e in client.get("/expenses", params={"status": "approved"}).json()]

    assert pending_ids == [pending["id"]]
    assert approved_ids == [approved["id"]]


def test_get_missing_expense_is_404(client: TestClient) -> None:
    response = client.get("/expenses/999")

    assert response.status_code == 404
    assert response.json() == {"detail": "expense not found"}


def test_approve_flips_status_to_approved(client: TestClient) -> None:
    expense = create(client)

    response = client.post(f"/expenses/{expense['id']}/approve")

    assert response.status_code == 200
    assert response.json()["status"] == "approved"
    assert client.get(f"/expenses/{expense['id']}").json()["status"] == "approved"


def test_reapproving_is_a_no_op(client: TestClient) -> None:
    """ARCHITECTURE.md 4.2: repeating the same decision is a 200 no-op, record unchanged."""
    expense = create(client)
    first = client.post(f"/expenses/{expense['id']}/approve")

    second = client.post(f"/expenses/{expense['id']}/approve")

    assert second.status_code == 200
    assert second.json() == first.json()


def test_rejecting_an_approved_expense_is_409(client: TestClient) -> None:
    """ARCHITECTURE.md 4.2: changing a decision is a 409, and the first decision stands."""
    expense = create(client)
    client.post(f"/expenses/{expense['id']}/approve")

    response = client.post(f"/expenses/{expense['id']}/reject", json={"reason": "duplicate"})

    assert response.status_code == 409
    assert response.json() == {"detail": "already approved"}
    assert client.get(f"/expenses/{expense['id']}").json()["status"] == "approved"


# --- X-API-Key auth on writes (POST /expenses, approve, reject). Reads stay open.


def test_write_without_api_key_is_401(client: TestClient, anon_client: TestClient) -> None:
    response = anon_client.post("/expenses", json=EXPENSE)

    assert response.status_code == 401
    assert response.json() == {"detail": "missing or invalid X-API-Key header"}
    # Refused before the handler ran, so nothing was saved.
    assert client.get("/expenses").json() == []


@pytest.mark.parametrize("action", ["approve", "reject"])
def test_decisions_without_api_key_are_401(
    client: TestClient, anon_client: TestClient, action: str
) -> None:
    expense = create(client)

    response = anon_client.post(f"/expenses/{expense['id']}/{action}", json={"reason": "x"})

    assert response.status_code == 401
    assert client.get(f"/expenses/{expense['id']}").json()["status"] == "pending"


def test_write_with_wrong_api_key_is_401(anon_client: TestClient) -> None:
    response = anon_client.post(
        "/expenses", json=EXPENSE, headers={"X-API-Key": "not-the-key"}
    )

    assert response.status_code == 401


def test_writes_fail_closed_when_server_has_no_api_key(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If API_KEY isn't configured, writes are refused, not left open."""
    monkeypatch.delenv("API_KEY")

    response = client.post("/expenses", json=EXPENSE)  # still sends the old test key

    assert response.status_code == 401


def test_reads_do_not_need_api_key(client: TestClient, anon_client: TestClient) -> None:
    expense = create(client)

    assert anon_client.get("/expenses").status_code == 200
    assert anon_client.get(f"/expenses/{expense['id']}").status_code == 200


# --- FX conversion (ARCHITECTURE.md 1 and 4.1), with the rate API faked at the transport.


FAKE_USD_INR_RATE: str = "83.25"


def fake_rate_payload() -> dict:
    """Body the fake rate API returns for USD -> INR.

    ASSUMPTION: ARCHITECTURE.md doesn't define the FX API's response format. Change this
    to match the real API once one is chosen; the success-path test depends on it.
    """
    return {"base": "USD", "rates": {"INR": FAKE_USD_INR_RATE}}


def test_fx_unreachable_saves_unconverted_expense(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARCHITECTURE.md 4.1: if the rate API is down, save anyway, return 202, INR amount NULL."""
    fx_calls: list[httpx.Request] = []

    def unreachable(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        fx_calls.append(request)
        raise httpx.ConnectError("FX API unreachable (test)", request=request)

    # Fails every real outbound request made with `httpx` (the FX client must use httpx,
    # per the stack in CLAUDE.md). TestClient is built on httpx2 with its own in-process
    # transport, so calls to the app still work.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", unreachable)

    response = client.post("/expenses", json={**EXPENSE, "currency": "USD"})

    # The patch must actually have been hit. Otherwise the test could pass because the
    # real FX API happens to be unreachable here, not because of the simulated outage.
    assert len(fx_calls) >= 1

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "pending"
    assert body["amount_base_minor"] is None

    # Re-read it: the expense must really be saved, with the failure recorded.
    stored = client.get(f"/expenses/{body['id']}")
    assert stored.status_code == 200
    saved = stored.json()
    assert saved["amount_minor"] == EXPENSE["amount_minor"]
    assert saved["currency"] == "USD"
    assert saved["amount_base_minor"] is None
    assert saved["fx_attempts"] >= 1  # >= because GET also retries while unconverted
    # Our simulated error, not some other exception swallowed as "FX down".
    assert "unreachable" in saved["fx_last_error"]


def test_fx_success_converts_to_inr_half_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Partner to the outage test: when FX works, the expense is converted and returns 201.

    Without this, an implementation that never calls FX and always returns 202 would pass
    the outage test.
    """
    fx_calls: list[httpx.Request] = []

    def rate_api(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        fx_calls.append(request)
        return httpx.Response(200, json=fake_rate_payload(), request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", rate_api)

    # $0.50 x 83.25 = ₹41.625 = 4162.5 paise. Half-up gives 4163; banker's rounding or
    # truncation would give 4162, so this also checks the rounding rule.
    response = client.post(
        "/expenses", json={**EXPENSE, "currency": "USD", "amount_minor": 50}
    )

    assert len(fx_calls) >= 1
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "pending"
    assert body["amount_base_minor"] == 4163
    assert body["amount_base_display"] == "₹41.63"
    assert body["fx_rate"] == FAKE_USD_INR_RATE
    assert body["fx_attempts"] == 1
    assert body["fx_last_error"] is None


# --- Fixes from code review.


def test_non_ascii_api_key_is_401_not_500(anon_client: TestClient) -> None:
    """Starlette decodes the header as Latin-1; a non-ASCII key must not crash compare_digest."""
    response = anon_client.post(
        "/expenses", json=EXPENSE, headers={"X-API-Key": b"caf\xe9"}
    )

    assert response.status_code == 401


@pytest.mark.parametrize("amount", [10**12 + 1, 10**19])
def test_amount_above_max_is_422(client: TestClient, amount: int) -> None:
    """Rejected at input, instead of a 500 from SQLite's integer overflow."""
    response = client.post("/expenses", json={**EXPENSE, "amount_minor": amount})

    assert response.status_code == 422


def test_conversion_too_large_to_store_is_202_not_500(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An absurd rate can't make the saved expense unreadable: it's recorded as an FX failure."""

    def absurd_rate(self: httpx.HTTPTransport, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"rates": {"INR": "1e18"}}, request=request)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", absurd_rate)

    response = client.post(
        "/expenses", json={**EXPENSE, "currency": "USD", "amount_minor": 10**12}
    )

    assert response.status_code == 202
    assert "too large" in response.json()["fx_last_error"]
    # Later reads retry the conversion and must still work.
    assert client.get(f"/expenses/{response.json()['id']}").status_code == 200


def test_insights_without_api_key_is_401(anon_client: TestClient) -> None:
    assert anon_client.get("/expenses/insights").status_code == 401


def test_insights_sends_only_newest_expenses(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At most MAX_INSIGHT_EXPENSES are sent, newest ones, in oldest-first order."""
    import app.routes as routes

    sent: list[list[dict]] = []

    def fake_generate_insight(expenses: list[dict]) -> dict:
        sent.append(expenses)
        return {"summary": "s", "bullets": ["a", "b", "c"], "source": "ai"}

    monkeypatch.setattr(routes, "generate_insight", fake_generate_insight)
    monkeypatch.setattr(routes, "MAX_INSIGHT_EXPENSES", 2)
    for name in ("first", "second", "third"):
        create(client, description=name)

    response = client.get("/expenses/insights")

    assert response.status_code == 200
    assert response.json()["insight"]["bullets"] == ["a", "b", "c"]
    assert [e["description"] for e in sent[0]] == ["second", "third"]


def test_recent_returns_expense_out_items(client: TestClient) -> None:
    for name in ("older", "newer"):
        create(client, description=name)

    body = client.get("/expenses/recent/1").json()

    assert body["latest"] == "NEWER"
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["description"] == "newer"
    assert item["amount_base_display"] == "₹1,250.50"  # ExpenseOut field, not a raw row
    assert "_sa_instance_state" not in item


def test_recent_with_no_expenses(client: TestClient) -> None:
    assert client.get("/expenses/recent/3").json() == {"latest": None, "items": []}


# --- PII masking in responses.

PII_DESCRIPTION: str = (
    "team lunch 1111 2222 3333 4444 amd contact 9845723112/ contact@example.com"
)
MASKED_DESCRIPTION: str = "team lunch [CARD] amd contact [PHONE]/ [EMAIL]"


def test_responses_mask_pii_but_database_keeps_original(client: TestClient) -> None:
    created = create(client, description=PII_DESCRIPTION, category="refund bob@example.com")

    assert created["description"] == MASKED_DESCRIPTION
    assert created["category"] == "refund [EMAIL]"
    assert client.get(f"/expenses/{created['id']}").json()["description"] == MASKED_DESCRIPTION
    assert client.get("/expenses").json()[0]["description"] == MASKED_DESCRIPTION
    approved = client.post(f"/expenses/{created['id']}/approve").json()
    assert approved["description"] == MASKED_DESCRIPTION

    # The stored row is untouched: masking happens only on the way out.
    db = next(app.dependency_overrides[get_db]())
    try:
        stored = db.get(Expense, created["id"])
        assert stored.description == PII_DESCRIPTION
    finally:
        db.close()


def test_recent_masks_pii(client: TestClient) -> None:
    create(client, description=PII_DESCRIPTION)

    body = client.get("/expenses/recent/1").json()

    assert body["latest"] == MASKED_DESCRIPTION.upper()
    assert body["items"][0]["description"] == MASKED_DESCRIPTION
    assert "1111" not in str(body) and "9845723112" not in str(body)
