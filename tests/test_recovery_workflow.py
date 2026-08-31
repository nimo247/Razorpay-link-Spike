import json
from datetime import date, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_database_session
from app.main import create_app
from app.models import (
    AuditEvent,
    Invoice,
    InvoiceStatus,
    PaymentPromise,
    PromiseStatus,
    WebhookEvent,
)
from app.razorpay_client import (
    RazorpayError,
    expected_webhook_signature,
)
def test_reconciliation_applies_paid_link_once(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    link_response, _ = create_mock_payment_link(
        client,
        promise_id,
    )

    assert link_response.status_code == 200

    external_link = {
        "id": "plink_test_exact",
        "status": "paid",
        "amount": 4_000_000,
        "amount_paid": 4_000_000,
    }

    with patch(
        "app.routes.promises.get_payment_link"
    ) as mocked_get:
        mocked_get.return_value = external_link

        first = client.post(
            f"/promises/{promise_id}/reconcile"
        )

        second = client.post(
            f"/promises/{promise_id}/reconcile"
        )

    assert first.status_code == 200
    assert first.json()["reconciled"] is True
    assert first.json()["already_applied"] is False
    assert first.json()["promise_status"] == "PAID"
    assert first.json()["outstanding_amount_paise"] == 800_000

    assert second.status_code == 200
    assert second.json()["reconciled"] is False
    assert second.json()["already_applied"] is True

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

        reconciliation_events = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.promise_id == promise_id,
                AuditEvent.event_type
                == "PAYMENT_RECONCILED",
            )
        )
    assert invoice is not None
    assert payment_promise is not None
    assert invoice.paid_amount_paise == 4_000_000
    assert invoice.outstanding_amount_paise == 800_000
    assert payment_promise.status == PromiseStatus.PAID
    assert reconciliation_events == 1


def test_reconciliation_rejects_unpaid_link(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    create_mock_payment_link(client, promise_id)

    with patch(
        "app.routes.promises.get_payment_link"
    ) as mocked_get:
        mocked_get.return_value = {
            "id": "plink_test_exact",
            "status": "created",
            "amount": 4_000_000,
            "amount_paid": 0,
        }

        response = client.post(
            f"/promises/{promise_id}/reconcile"
        )

    assert response.status_code == 409
    assert "not paid" in response.json()["detail"]

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

    assert invoice is not None
    assert payment_promise is not None
    assert invoice.paid_amount_paise == 0
    assert payment_promise.status == PromiseStatus.LINK_CREATED


def test_reconciliation_amount_mismatch_moves_to_review(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    create_mock_payment_link(client, promise_id)

    with patch(
        "app.routes.promises.get_payment_link"
    ) as mocked_get:
        mocked_get.return_value = {
            "id": "plink_test_exact",
            "status": "paid",
            "amount": 4_000_000,
            "amount_paid": 3_900_000,
        }

        response = client.post(
            f"/promises/{promise_id}/reconcile"
        )

    assert response.status_code == 200
    assert response.json()["reconciled"] is False
    assert response.json()["human_review"] is True
    assert response.json()["reason"] == (
        "Payment amount mismatch"
    )

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

    assert invoice is not None
    assert payment_promise is not None
    assert invoice.paid_amount_paise == 0
    assert invoice.status == InvoiceStatus.HUMAN_REVIEW
    assert payment_promise.status == PromiseStatus.HUMAN_REVIEW


def test_reconciliation_handles_razorpay_failure(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    create_mock_payment_link(client, promise_id)

    with patch(
        "app.routes.promises.get_payment_link",
        side_effect=RazorpayError(
            "Razorpay temporarily unavailable"
        ),
    ):
        response = client.post(
            f"/promises/{promise_id}/reconcile"
        )

    assert response.status_code == 502

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

    assert invoice is not None
    assert payment_promise is not None
    assert invoice.paid_amount_paise == 0
    assert payment_promise.status == PromiseStatus.LINK_CREATED


@pytest.fixture()
def recovery_environment(monkeypatch):
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

    monkeypatch.setenv("RAZORPAY_KEY_ID", "rzp_test_fake")
    monkeypatch.setenv("RAZORPAY_KEY_SECRET", "fake-key-secret")
    monkeypatch.setenv(
        "RAZORPAY_WEBHOOK_SECRET",
        "fake-webhook-secret",
    )

    def override_database_session():
        session = TestingSession()

        try:
            yield session
        finally:
            session.close()

    application = create_app()
    application.dependency_overrides[
        get_database_session
    ] = override_database_session

    client = TestClient(application)

    yield client, TestingSession

    client.close()
    Base.metadata.drop_all(bind=test_engine)
    test_engine.dispose()


def create_invoice(client: TestClient) -> str:
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

    return response.json()["id"]


def create_validated_promise(
    client: TestClient,
    invoice_id: str,
) -> str:
    response = client.post(
        f"/invoices/{invoice_id}/promises",
        json={
            "customer_message": (
                "I can pay 40k this Friday. "
                "The other 8k is disputed."
            ),
            "promised_amount_paise": 4_000_000,
            "disputed_amount_paise": 800_000,
            "promised_date": (
                date.today() + timedelta(days=4)
            ).isoformat(),
            "evidence_quotes": [
                "pay 40k this Friday",
                "other 8k is disputed",
            ],
        },
    )

    assert response.status_code == 201
    assert response.json()["status"] == "VALIDATED"

    return response.json()["id"]


def create_mock_payment_link(
    client: TestClient,
    promise_id: str,
):
    with patch(
        "app.routes.promises.create_payment_link"
    ) as mocked_create:
        mocked_create.return_value = {
            "id": "plink_test_exact",
            "short_url": "https://rzp.io/test-link",
            "status": "created",
            "amount": 4_000_000,
            "accept_partial": False,
        }

        response = client.post(
            f"/promises/{promise_id}/payment-link"
        )

        return response, mocked_create.call_count


def send_webhook(
    client: TestClient,
    *,
    event_id: str,
    event_type: str,
    amount_paid: int,
):
    payload = {
        "event": event_type,
        "payload": {
            "payment_link": {
                "entity": {
                    "id": "plink_test_exact",
                    "amount": 4_000_000,
                    "amount_paid": amount_paid,
                    "status": (
                        "paid"
                        if event_type == "payment_link.paid"
                        else "partially_paid"
                    ),
                }
            }
        },
    }

    raw_body = json.dumps(
        payload,
        separators=(",", ":"),
    ).encode()

    signature = expected_webhook_signature(
        raw_body,
        "fake-webhook-secret",
    )

    return client.post(
        "/webhooks/razorpay",
        content=raw_body,
        headers={
            "Content-Type": "application/json",
            "X-Razorpay-Signature": signature,
            "X-Razorpay-Event-Id": event_id,
        },
    )


def test_ungrounded_evidence_is_rejected(
    recovery_environment,
) -> None:
    client, _ = recovery_environment
    invoice_id = create_invoice(client)

    response = client.post(
        f"/invoices/{invoice_id}/promises",
        json={
            "customer_message": "I can pay 40k Friday.",
            "promised_amount_paise": 4_000_000,
            "disputed_amount_paise": 0,
            "promised_date": (
                date.today() + timedelta(days=4)
            ).isoformat(),
            "evidence_quotes": ["₹40,000 on Friday"],
        },
    )

    assert response.status_code == 400
    assert "exact input substring" in response.json()["detail"]


def test_amount_exceeding_balance_is_rejected(
    recovery_environment,
) -> None:
    client, _ = recovery_environment
    invoice_id = create_invoice(client)

    response = client.post(
        f"/invoices/{invoice_id}/promises",
        json={
            "customer_message": "I will pay 50k Friday.",
            "promised_amount_paise": 5_000_000,
            "disputed_amount_paise": 0,
            "promised_date": (
                date.today() + timedelta(days=4)
            ).isoformat(),
            "evidence_quotes": ["pay 50k Friday"],
        },
    )

    assert response.status_code == 400
    assert "exceed" in response.json()["detail"].lower()


def test_repeated_link_request_reuses_existing_link(
    recovery_environment,
) -> None:
    client, _ = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    with patch(
        "app.routes.promises.create_payment_link"
    ) as mocked_create:
        mocked_create.return_value = {
            "id": "plink_test_exact",
            "short_url": "https://rzp.io/test-link",
            "status": "created",
            "amount": 4_000_000,
            "accept_partial": False,
        }

        first = client.post(
            f"/promises/{promise_id}/payment-link"
        )

        second = client.post(
            f"/promises/{promise_id}/payment-link"
        )

    assert first.status_code == 200
    assert second.status_code == 200

    assert first.json()["reused"] is False
    assert second.json()["reused"] is True

    assert (
        first.json()["payment_link_id"]
        == second.json()["payment_link_id"]
    )

    assert mocked_create.call_count == 1


def test_paid_webhook_updates_workflow_once(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    link_response, call_count = create_mock_payment_link(
        client,
        promise_id,
    )

    assert link_response.status_code == 200
    assert call_count == 1

    first_webhook = send_webhook(
        client,
        event_id="event_paid_001",
        event_type="payment_link.paid",
        amount_paid=4_000_000,
    )

    replayed_webhook = send_webhook(
        client,
        event_id="event_paid_001",
        event_type="payment_link.paid",
        amount_paid=4_000_000,
    )

    assert first_webhook.status_code == 200
    assert first_webhook.json()["promise_status"] == "PAID"
    assert first_webhook.json()["invoice_status"] == "DISPUTED"

    assert replayed_webhook.status_code == 200
    assert replayed_webhook.json()["duplicate"] is True

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

        payment_event_count = session.scalar(
            select(func.count())
            .select_from(AuditEvent)
            .where(
                AuditEvent.invoice_id == invoice_id,
                AuditEvent.event_type == "PAYMENT_RECEIVED",
            )
        )

        webhook_event_count = session.scalar(
            select(func.count())
            .select_from(WebhookEvent)
            .where(
                WebhookEvent.event_id == "event_paid_001"
            )
        )

    assert invoice is not None
    assert payment_promise is not None

    assert invoice.status == InvoiceStatus.DISPUTED
    assert invoice.paid_amount_paise == 4_000_000
    assert invoice.outstanding_amount_paise == 800_000
    assert invoice.disputed_amount_paise == 800_000

    assert payment_promise.status == PromiseStatus.PAID

    assert payment_event_count == 1
    assert webhook_event_count == 1


def test_payment_amount_mismatch_moves_to_review(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    link_response, _ = create_mock_payment_link(
        client,
        promise_id,
    )

    assert link_response.status_code == 200

    response = send_webhook(
        client,
        event_id="event_mismatch_001",
        event_type="payment_link.paid",
        amount_paid=3_900_000,
    )

    assert response.status_code == 200
    assert response.json()["human_review"] is True

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

    assert invoice is not None
    assert payment_promise is not None

    assert invoice.status == InvoiceStatus.HUMAN_REVIEW
    assert invoice.paid_amount_paise == 0
    assert invoice.outstanding_amount_paise == 4_800_000

    assert (
        payment_promise.status
        == PromiseStatus.HUMAN_REVIEW
    )


def test_unexpected_partial_event_moves_to_review(
    recovery_environment,
) -> None:
    client, TestingSession = recovery_environment

    invoice_id = create_invoice(client)
    promise_id = create_validated_promise(
        client,
        invoice_id,
    )

    link_response, _ = create_mock_payment_link(
        client,
        promise_id,
    )

    assert link_response.status_code == 200

    response = send_webhook(
        client,
        event_id="event_partial_001",
        event_type="payment_link.partially_paid",
        amount_paid=2_000_000,
    )

    assert response.status_code == 200
    assert response.json()["human_review"] is True

    with TestingSession() as session:
        invoice = session.get(Invoice, invoice_id)
        payment_promise = session.get(
            PaymentPromise,
            promise_id,
        )

        review_event = session.scalar(
            select(AuditEvent).where(
                AuditEvent.invoice_id == invoice_id,
                AuditEvent.event_type
                == "UNEXPECTED_PARTIAL_PAYMENT",
            )
        )

    assert invoice is not None
    assert payment_promise is not None
    assert review_event is not None

    assert invoice.status == InvoiceStatus.HUMAN_REVIEW
    assert (
        payment_promise.status
        == PromiseStatus.HUMAN_REVIEW
    )