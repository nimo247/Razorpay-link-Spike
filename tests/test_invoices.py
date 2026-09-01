from datetime import date, timedelta
from pathlib import Path
import tempfile

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_database_session
from app.main import create_app
from app.models import AuditEvent


test_engine = create_engine(
    "sqlite+pysqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)

TestingSession = sessionmaker(
    bind=test_engine,
    autoflush=False,
    expire_on_commit=False,
)

Base.metadata.create_all(bind=test_engine)


def override_database_session():
    session = TestingSession()

    try:
        yield session
    finally:
        session.close()


def build_client() -> TestClient:
    temporary_directory = tempfile.TemporaryDirectory()

    app = create_app(
        Path(temporary_directory.name) / "webhook-events.db"
    )

    app.state.test_temporary_directory = temporary_directory
    app.dependency_overrides[
        get_database_session
    ] = override_database_session

    return TestClient(app)


def test_create_overdue_invoice() -> None:
    client = build_client()

    response = client.post(
        "/invoices",
        json={
            "customer_name": "Acme Stores",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    assert response.status_code == 201

    result = response.json()

    assert result["customer_name"] == "Acme Stores"
    assert result["original_amount_paise"] == 4_800_000
    assert result["outstanding_amount_paise"] == 4_800_000
    assert result["paid_amount_paise"] == 0
    assert result["status"] == "OVERDUE"


def test_invoice_creation_adds_audit_event() -> None:
    client = build_client()

    response = client.post(
        "/invoices",
        json={
            "customer_name": "Acme Stores",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    invoice_id = response.json()["id"]

    with TestingSession() as session:
        events = session.scalars(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice_id
            )
        ).all()

    assert len(events) == 1
    assert events[0].event_type == "INVOICE_CREATED"


def test_future_invoice_is_rejected() -> None:
    client = build_client()

    response = client.post(
        "/invoices",
        json={
            "customer_name": "Acme Stores",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() + timedelta(days=1)
            ).isoformat(),
        },
    )

    assert response.status_code == 400


def test_zero_amount_is_rejected() -> None:
    client = build_client()

    response = client.post(
        "/invoices",
        json={
            "customer_name": "Acme Stores",
            "original_amount_paise": 0,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    assert response.status_code == 422


def test_get_invoice() -> None:
    client = build_client()

    created = client.post(
        "/invoices",
        json={
            "customer_name": "Acme Stores",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    invoice_id = created.json()["id"]

    response = client.get(f"/invoices/{invoice_id}")

    assert response.status_code == 200
    assert response.json()["id"] == invoice_id


def test_missing_invoice_returns_404() -> None:
    client = build_client()

    response = client.get(
        "/invoices/00000000-0000-0000-0000-000000000000"
    )

    assert response.status_code == 404

def test_list_invoices_contains_created_invoice() -> None:
    client = build_client()

    created = client.post(
        "/invoices",
        json={
            "customer_name": "Dashboard Merchant",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    assert created.status_code == 201
    invoice_id = created.json()["id"]

    response = client.get("/invoices")

    assert response.status_code == 200

    invoices = response.json()

    assert isinstance(invoices, list)

    matching_invoice = next(
        invoice
        for invoice in invoices
        if invoice["id"] == invoice_id
    )

    assert matching_invoice["customer_name"] == "Dashboard Merchant"
    assert matching_invoice["status"] == "OVERDUE"


def test_invoice_workspace_returns_promise_and_audit_trail() -> None:
    client = build_client()

    created_invoice = client.post(
        "/invoices",
        json={
            "customer_name": "Workspace Merchant",
            "original_amount_paise": 4_800_000,
            "due_date": (
                date.today() - timedelta(days=10)
            ).isoformat(),
        },
    )

    assert created_invoice.status_code == 201
    invoice_id = created_invoice.json()["id"]

    created_promise = client.post(
        f"/invoices/{invoice_id}/promises",
        json={
            "customer_message": (
                "I can pay 40k this Friday. "
                "The other 8k is disputed."
            ),
            "promised_amount_paise": 4_000_000,
            "disputed_amount_paise": 800_000,
            "promised_date": (
                date.today() + timedelta(days=7)
            ).isoformat(),
            "evidence_quotes": [
                "40k",
                "this Friday",
                "8k is disputed",
            ],
        },
    )

    assert created_promise.status_code == 201
    promise_id = created_promise.json()["id"]

    response = client.get(
        f"/invoices/{invoice_id}/workspace"
    )

    assert response.status_code == 200

    workspace = response.json()

    assert workspace["invoice"]["id"] == invoice_id
    assert workspace["invoice"]["status"] == "DISPUTED"
    assert workspace["promise"]["id"] == promise_id
    assert workspace["promise"]["status"] == "VALIDATED"

    event_types = [
        event["event_type"]
        for event in workspace["audit_events"]
    ]

    assert "INVOICE_CREATED" in event_types
    assert "PROMISE_PROPOSED" in event_types
    assert "PROMISE_VALIDATED" in event_types

    timestamps = [
        event["created_at"]
        for event in workspace["audit_events"]
    ]

    assert timestamps == sorted(timestamps)